import asyncio
import random
import json
import pickle
import logging
import time
import datetime as dt
from pathlib import Path
import re
import copy

from collections import defaultdict
from recordtype import recordtype
from datetime import datetime

import discord
from discord.ext import commands

from remind.util.rounds import Round
from remind.util import discord_common
from remind.util import paginator
from remind import constants
from remind.util import clist_api as clist
from remind.util.website_schema import WebsitePatterns
from remind.util import website_schema


class RemindersCogError(commands.CommandError):
    pass


_CONTESTS_PER_PAGE = 5
_CONTEST_PAGINATE_WAIT_TIME = 5 * 60
_FINISHED_CONTESTS_LIMIT = 5
_CONTEST_REFRESH_PERIOD = 10 * 60  # seconds
_GUILD_SETTINGS_BACKUP_PERIOD = 6 * 60 * 60  # seconds

GuildSettings = recordtype(
    'GuildSettings', [
        ('remind_channel_id', None), ('remind_role_id', None), ('remind_before', None),
        ('finalcall_channel_id', None), ('finalcall_before', None),
        ('auto_nope_react', False),
        ('add_first_reaction', False),
        ('website_patterns', defaultdict(WebsitePatterns))])


class RemindRequest:
    def __init__(self, channel, role, contest: Round, before_secs, send_time):
        self.channel = channel
        self.role = role
        self.contest = contest
        self.before_secs = before_secs
        self.send_time = send_time


class FinalCallRequest:
    def __init__(self, *, embed, role_id, msg_id=None):
        self.role_id = role_id
        self.msg_id = msg_id
        self.embed_desc = embed.description
        self.embed_fields = [(field.name, field.value) for field in embed.fields]


def get_default_guild_settings():
    settings = GuildSettings()
    settings.website_patterns = copy.deepcopy(website_schema.schema)
    return settings


def _contest_start_time_format(contest):
    seconds = int(contest.start_time.replace(tzinfo=dt.timezone.utc).timestamp())
    return f'<t:{seconds}:F>'


def _contest_duration_format(contest):
    duration_days, duration_hrs, duration_mins, _ = discord_common.time_format(contest.duration.total_seconds())
    duration = f'{duration_hrs}h {duration_mins}m'
    if duration_days > 0:
        duration = f'{duration_days}d ' + duration
    return duration


def _get_formatted_contest_desc(start, duration, url):
    em = '\N{EN SPACE}'
    return f'{start}\nDuration:{em}{duration}{em}|{em}[link]({url})'


def _get_contest_website_prefix(contest):
    website_details = website_schema.schema[contest.website]
    return website_details.prefix


def _get_display_name(website, name):
    return (website + " || " + name) if website.lower() not in name.lower() else name


def _get_embed_fields_from_contests(contests):
    fields = []
    for contest in contests:
        start = _contest_start_time_format(contest)
        duration = _contest_duration_format(contest)
        value = _get_formatted_contest_desc(start, duration, contest.url)
        website = _get_contest_website_prefix(contest)
        fields.append((website, contest.name, value))
    return fields


async def _send_reminder_at(request):
    delay = request.send_time - dt.datetime.utcnow().timestamp()
    if delay <= 0:
        return

    await asyncio.sleep(delay)
    values = discord_common.time_format(request.before_secs)

    def make(value, label):
        tmp = f'{value} {label}'
        return tmp if value == 1 else tmp + 's'

    labels = 'day hr min sec'.split()
    before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
    desc = f'About to start in {before_str}!'
    embed = discord_common.color_embed(description=desc)
    if request.contest.is_rare():
        embed.set_footer(text=f"Its once in a while contest, you wouldn't wanna miss 👀")
    for website, name, value in _get_embed_fields_from_contests([request.contest]):
        embed.add_field(name=_get_display_name(website, name), value=value, inline=False)
    await request.channel.send(request.role.mention + f' Its {website} time!', embed=embed)


def filter_contests(filters, contests):
    if not filters:
        return contests

    filtered_contests = []
    for contest in contests:
        eligible = False
        for contest_filter in filters:
            if contest_filter[0] == "+":
                filter = contest_filter[1:]
                for website, data in website_schema.schema.items():
                    eligible |= (website == contest.website and filter in data.shorthands)
        if eligible:
            filtered_contests.append(contest)
    return filtered_contests


def create_tuple_defaultdict():
    return defaultdict(FinalCallRequest)


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.future_contests = None
        self.contest_cache = None
        self.active_contests = None
        self.finished_contests = None
        self.start_time_map = defaultdict(list)
        self.task_map = defaultdict(list)
        # Maps guild_id to `GuildSettings`
        self.guild_map = defaultdict(get_default_guild_settings)
        self.last_guild_backup_time = -1
        self.reaction_emoji = "✅"
        self.nope_emoji = 973583086174498847

        self.finalcall_map = defaultdict(create_tuple_defaultdict)
        self.finaltasks = defaultdict(lambda: dict())

        self.member_converter = commands.MemberConverter()
        self.role_converter = commands.RoleConverter()

        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        guild_map_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        try:
            with guild_map_path.open('rb') as guild_map_file:
                data = pickle.load(guild_map_file)
                guild_map = data["guild_map"]
                self.finalcall_map = data["finalcall_map"]
                for guild_id, guild_settings in guild_map.items():
                    self.guild_map[guild_id] = GuildSettings(**{key: value
                                                                for key, value
                                                                in guild_settings._asdict().items()
                                                                if key in GuildSettings._fields})
        except BaseException:
            pass
        asyncio.create_task(self._update_task())

    async def cog_after_invoke(self, ctx):
        self._serialize_guild_map()
        self._backup_serialize_guild_map()
        self._reschedule_reminder_tasks(ctx.guild.id)
        self._reschedule_finalcall_tasks(ctx.guild.id)

    async def _update_task(self):
        self.logger.info(f'Invoking Scheduled Reminder Updates')
        self._generate_contest_cache()
        contest_cache = self.contest_cache
        current_time = dt.datetime.utcnow()

        self.future_contests = [
            contest for contest in contest_cache
            if contest.start_time > current_time
        ]
        self.finished_contests = [
            contest for contest in contest_cache
            if contest.start_time + contest.duration < current_time
        ]
        self.active_contests = [
            contest for contest in contest_cache
            if contest.start_time <= current_time <= contest.start_time + contest.duration
        ]

        self.active_contests.sort(key=lambda contest: contest.start_time)
        self.finished_contests.sort(key=lambda contest: contest.start_time + contest.duration, reverse=True)
        self.future_contests.sort(key=lambda contest: contest.start_time)
        # Keep most recent _FINISHED_LIMIT
        self.finished_contests = self.finished_contests[:_FINISHED_CONTESTS_LIMIT]
        self.start_time_map.clear()
        for contest in self.future_contests:
            self.start_time_map[time.mktime(contest.start_time.timetuple())].append(contest)
        self._reschedule_all_tasks()
        await asyncio.sleep(_CONTEST_REFRESH_PERIOD)
        asyncio.create_task(self._update_task())

    def _generate_contest_cache(self):
        clist.cache(forced=False)
        db_file = Path(constants.CONTESTS_DB_FILE_PATH)
        with db_file.open() as f:
            data = json.load(f)
        contests = [Round(contest) for contest in data['objects']]
        self.contest_cache = [contest for contest in contests if contest.is_desired(website_schema.schema)]

    def get_guild_contests(self, contests, guild_id):
        settings = self.guild_map[guild_id]

        desired_contests = []
        for contest in contests:
            if contest.is_desired(settings.website_patterns):
                desired_contests.append(contest)

        return desired_contests

    def _reschedule_all_tasks(self):
        for guild in self.bot.guilds:
            self._reschedule_reminder_tasks(guild.id)
            self._reschedule_finalcall_tasks(guild.id)

    def _reschedule_reminder_tasks(self, guild_id):
        for task in self.task_map[guild_id]:
            task.cancel()
        self.task_map[guild_id].clear()
        self.logger.info(f'Tasks for guild "{self.bot.get_guild(guild_id)}" cleared')

        if not self.start_time_map:
            return

        settings = self.guild_map[guild_id]
        if settings.remind_role_id is None:
            return

        guild = self.bot.get_guild(guild_id)
        channel, role = guild.get_channel(settings.remind_channel_id), guild.get_role(settings.remind_role_id)

        for start_time, contests in self.start_time_map.items():
            contests = self.get_guild_contests(contests, guild_id)
            if not contests:
                continue

            website_seggregated_contests = dict()
            for contest in contests:
                website_seggregated_contests[contest.url] = contest  # an url can uniquely identify a contest

            for _, seg_contest in website_seggregated_contests.items():
                for before_mins in settings.remind_before:
                    before_secs = 60 * before_mins
                    request = RemindRequest(channel, role, seg_contest, before_secs, start_time - before_secs)
                    task = asyncio.create_task(_send_reminder_at(request))
                    self.task_map[guild_id].append(task)

        self.logger.info(
            f'{len(self.task_map[guild_id])} reminder tasks scheduled for guild "{self.bot.get_guild(guild_id)}"')

    def _reschedule_finalcall_tasks(self, guild_id):
        if not self.finalcall_map[guild_id]:
            return

        pending_reschedule = []
        for link, data in self.finalcall_map[guild_id].items():
            try:
                pending_reschedule.append(data)
                task = self.finaltasks[guild_id][link]
                task.cancel()
            except KeyError:
                pass

        self.finalcall_map[guild_id].clear()

        for data in pending_reschedule:
            embed_desc, embed_fields = data.embed_desc, data.embed_fields
            embed = discord_common.color_embed()
            embed.description = embed_desc
            for (name, value) in embed_fields:
                embed.add_field(name=name, value=value, inline=False)
            link, start_time = self.get_values_from_embed(embed)
            send_time = start_time - self.guild_map[guild_id].finalcall_before * 60
            reaction_role = self.bot.get_guild(guild_id).get_role(data.role_id)
            if reaction_role is not None:
                task = asyncio.create_task(
                    self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link))
                self.finalcall_map[guild_id][link] = FinalCallRequest(role_id=reaction_role.id, embed=embed,
                                                                      msg_id=data.msg_id)
                self.finaltasks[guild_id][link] = task

        self.logger.info(
            f'{len(self.finalcall_map[guild_id])} final calls scheduled for guild "{self.bot.get_guild(guild_id)}"')

    @staticmethod
    def _make_contest_pages(contests, title):
        pages = []
        chunks = paginator.chunkify(contests, _CONTESTS_PER_PAGE)
        for chunk in chunks:
            embed = discord_common.color_embed()
            for website, name, value in _get_embed_fields_from_contests(chunk):
                embed.add_field(name=_get_display_name(website, name), value=value, inline=False)
            pages.append((title, embed))
        return pages

    async def _send_contest_list(self, ctx, contests, *, title, empty_msg):
        if contests is None:
            raise RemindersCogError('Contest list not present')
        if len(contests) == 0:
            await ctx.send(embed=discord_common.embed_neutral(empty_msg))
            return
        pages = self._make_contest_pages(contests, title)
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=_CONTEST_PAGINATE_WAIT_TIME,
                           set_pagenum_footers=True)

    def _serialize_guild_map(self):
        self.logger.info("Serializing db to local file")
        data = {"guild_map": self.guild_map, "finalcall_map": self.finalcall_map}
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        with out_path.open(mode='wb') as out_file:
            pickle.dump(data, out_file)

    def _backup_serialize_guild_map(self):
        current_time_stamp = int(dt.datetime.utcnow().timestamp())
        if current_time_stamp - self.last_guild_backup_time < _GUILD_SETTINGS_BACKUP_PERIOD:
            return

        self.last_guild_backup_time = current_time_stamp
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH + "_" + str(current_time_stamp))
        data = {"guild_map": self.guild_map, "finalcall_map": self.finalcall_map}
        with out_path.open(mode='wb') as out_file:
            pickle.dump(data, out_file)

    @commands.group(brief='Commands for contest reminders', invoke_without_command=True)
    async def remind(self, ctx):
        await ctx.send_help(ctx.command)

    @remind.command(name='here', brief='Set reminder settings')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_remind_settings(self, ctx, role: discord.Role, *before: int):
        """Sets reminder channel to current channel,
        role to the given role, and reminder
        times to the given values in minutes.

        e.g t;remind here @Subscriber 10 60 180
        """
        if not role.mentionable:
            raise RemindersCogError('The role for reminders must be mentionable')
        if not before or any(before_mins < 0 for before_mins in before):
            raise RemindersCogError('Please provide valid `before` values')

        before = list(before)
        before = sorted(before, reverse=True)
        self.guild_map[ctx.guild.id].remind_channel_id = ctx.channel.id
        self.guild_map[ctx.guild.id].remind_role_id = role.id
        self.guild_map[ctx.guild.id].remind_before = before

        remind_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].remind_channel_id)
        remind_role = ctx.guild.get_role(self.guild_map[ctx.guild.id].remind_role_id)
        remind_before_str = f"At {', '.join(str(mins) for mins in self.guild_map[ctx.guild.id].remind_before)} " \
                            f"mins before contest "

        embed = discord_common.embed_success('Reminder settings saved successfully')
        embed.add_field(name='Reminder channel', value=remind_channel.mention)
        embed.add_field(name='Reminder Role', value=remind_role.mention)
        embed.add_field(name='Reminder Before', value=remind_before_str)

        await ctx.send(embed=embed)

    @remind.command(brief='Resets the subscribed websites to the default ones')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def reset_subscriptions(self, ctx):
        """ Resets the judges settings to the default ones.
        """
        self.guild_map[ctx.guild.id].website_patterns = copy.deepcopy(website_schema.schema)
        await ctx.send(embed=discord_common.embed_success('Succesfully reset the subscriptions to the default ones'))

    def _set_guild_setting(self, guild_id, websites, unsubscribe):

        guild_settings = self.guild_map[guild_id]
        supported_websites, unsupported_websites = [], []
        for website in websites:
            if website not in website_schema.schema:
                unsupported_websites.append(website)
                continue

            guild_settings.website_patterns[website].allowed_patterns = [] if unsubscribe else \
                copy.deepcopy(website_schema.schema[website].allowed_patterns)
            guild_settings.website_patterns[website].disallowed_patterns = [''] if unsubscribe else \
                copy.deepcopy(website_schema.schema[website].disallowed_patterns)
            supported_websites.append(website)

        self.guild_map[guild_id] = guild_settings
        return supported_websites, unsupported_websites

    @remind.command(brief='Start contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def subscribe(self, ctx, *websites: str):
        """Start contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.schema)
            embed = discord_common.embed_alert(
                f'None of these websites are supported for contest reminders.'
                f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            subscribed, unsupported = self._set_guild_setting(guild_id, websites, False)
            subscribed_websites_str = ", ".join(subscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully subscribed from  {subscribed_websites_str} for contest reminders.'
            success_str += f'\n{unsupported_websites_str} {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Stop contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def unsubscribe(self, ctx, *websites: str):
        """Stop contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.schema)
            embed = discord_common.embed_alert(f'None of these websites are supported for contest reminders.'
                                               f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            unsubscribed, unsupported = self._set_guild_setting(guild_id, websites, True)
            unsubscribed_websites_str = ", ".join(unsubscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully unsubscribed from {unsubscribed_websites_str} for contest reminders.'
            success_str += f'\n{unsupported_websites_str} \
                {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Clear all reminder settings')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def clear(self, ctx):
        del self.guild_map[ctx.guild.id]
        await ctx.send(embed=discord_common.embed_success('Reminder settings cleared'))

    @commands.group(brief='Commands for listing contests', invoke_without_command=True)
    async def clist(self, ctx):
        """
        Show past, present and future contests.Use filters to get contests from specific website

        Supported Filters : +cf/+codeforces +ac/+atcoder +cc/+codechef +hackercup +google +usaco +leetcode

        Eg: t;clist future +ac +codeforces
        will show contests from atcoder and codeforces
        """
        await ctx.send_help(ctx.command)

    @clist.command(brief='List future contests')
    async def future(self, ctx, *filters):
        """List future contests.
        """
        contests = filter_contests(filters, self.get_guild_contests(self.future_contests, ctx.guild.id))
        await self._send_contest_list(ctx, contests, title='Future contests', empty_msg='No future contests scheduled')

    @clist.command(brief='List active contests')
    async def active(self, ctx, *filters):
        """List active contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.active_contests, ctx.guild.id))
        await self._send_contest_list(ctx, contests, title='Active contests', empty_msg='No contests currently active')

    @clist.command(brief='List recent finished contests')
    async def finished(self, ctx, *filters):
        """List recently concluded contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.finished_contests, ctx.guild.id))
        await self._send_contest_list(ctx, contests, title='Recently finished contests',
                                      empty_msg='No finished contests found')

    async def send_finalcall_reminder(self, embed, guild_id, role, send_time, link):
        send_msg = "GLHF!"
        settings = self.guild_map[guild_id]

        # sleep till the ping time
        delay = send_time - dt.datetime.now().timestamp()
        if delay >= 0:
            await asyncio.sleep(delay)

            def make(value, label):
                tmp = f'{value} {label}'
                return tmp if value == 1 else tmp + 's'

            labels = 'day hr min sec'.split()
            values = discord_common.time_format(settings.finalcall_before * 60)
            before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
            desc = f'About to start in {before_str}!'
            embed.description = desc
            channel = self.bot.get_channel(settings.finalcall_channel_id)
            msg = await channel.send(role.mention + " " + send_msg, embed=embed)
            self.finalcall_map[guild_id][link].msg_id = msg.id
            self._serialize_guild_map()

        # sleep till contest starts
        time_to_contest = max(0, send_time + settings.finalcall_before * 60 - dt.datetime.utcnow().timestamp())
        await asyncio.sleep(time_to_contest)

        # delete role and task
        if link in self.finalcall_map[guild_id]:
            msg_id = self.finalcall_map[guild_id][link].msg_id
            message = await self.bot.get_channel(settings.finalcall_channel_id).fetch_message(msg_id)
            await message.edit(content=send_msg)
            del self.finalcall_map[guild_id][link]
            del self.finaltasks[guild_id][link]
        if role is not None:
            await role.delete()
        self._serialize_guild_map()

    @staticmethod
    def get_values_from_embed(embed):
        desc = embed.fields[0].value
        link = re.findall(r']\((http.+)\)', desc)[0]
        start_time = int(re.findall(r'<t:(\d+):[A-za-z]>', desc)[0])
        return link, start_time

    async def create_finalcall_role(self, guild_id, embed):
        contest_name = embed.fields[0].name
        name = f"Final Call - {contest_name}"
        role = await self.bot.get_guild(guild_id).create_role(name=name)
        return role

    async def get_finalcall_taskrole(self, guild_id, embed, remove=False):
        guild = self.bot.get_guild(guild_id)
        link, start_time = self.get_values_from_embed(embed)
        send_time = start_time - self.guild_map[guild_id].finalcall_before * 60

        if link in self.finalcall_map[guild_id]:
            reaction_role = guild.get_role(self.finalcall_map[guild_id][link].role_id)
        elif (not remove) and send_time > dt.datetime.utcnow().timestamp():
            reaction_role = await self.create_finalcall_role(guild_id, embed)
            task = asyncio.create_task(self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link))
            self.finalcall_map[guild_id][link] = FinalCallRequest(embed=embed, role_id=reaction_role.id)
            self.finaltasks[guild_id][link] = task
        else:
            reaction_role = None

        return reaction_role

    async def do_validation_check(self, payload):
        settings = self.guild_map[payload.guild_id]
        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        if member.bot or settings.remind_channel_id is None or settings.remind_channel_id != payload.channel_id \
            or payload.emoji.name != self.reaction_emoji or settings.finalcall_channel_id is None:
            return None

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        reaction_count = sum(reaction.count for reaction in message.reactions if str(reaction) == self.reaction_emoji)

        if not message.embeds:
            return None

        return reaction_count, message.embeds[0]

    async def victim_card(self, member):
        self.logger.error(f'Failed to send DM to {member}')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        response = await self.do_validation_check(payload)
        if response is None:
            return

        _, embed = response
        _, start_time = self.get_values_from_embed(embed)
        send_time = start_time - self.guild_map[payload.guild_id].finalcall_before * 60

        if send_time < dt.datetime.utcnow().timestamp():
            return

        settings = self.guild_map[payload.guild_id]
        reaction_role = await self.get_finalcall_taskrole(payload.guild_id, embed)
        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        self.logger.info(
            f'{member} reacted for {reaction_role} which will be sent at {datetime.fromtimestamp(send_time)}')
        await member.add_roles(reaction_role)
        member_dm = await member.create_dm()
        self._serialize_guild_map()
        try:
            await member_dm.send(f"Final Call Alarm Set. You are alloted `{reaction_role.name}` which will be pinged"
                                 f" {settings.finalcall_before} mins before the contest")
        except:
            await self.victim_card(member)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        response = await self.do_validation_check(payload)
        if response is None:
            return

        reaction_count, embed = response
        reaction_role = await self.get_finalcall_taskrole(payload.guild_id, embed, True)

        link, _ = self.get_values_from_embed(embed)
        if reaction_role is None:
            assert link not in self.finalcall_map[payload.guild_id]
            return

        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        self.logger.info(f'{member} unreacted for {reaction_role.name}')
        await member.remove_roles(reaction_role)
        member_dm = await member.create_dm()
        try:
            await member_dm.send(f"Final Call Alarm Cleared for '{reaction_role.name}'")
        except:
            await self.victim_card(member)

        if reaction_count == 1:
            if link in self.finalcall_map[payload.guild_id]:
                self.finaltasks[payload.guild_id][link].cancel()
                del self.finalcall_map[payload.guild_id][link]
                del self.finaltasks[payload.guild_id][link]
            await reaction_role.delete()
        self._serialize_guild_map()

    #  Nope React Command Group
    @commands.group(brief="Manage reactions in case of no reacts", invoke_without_command=True)
    @commands.has_role('Prabh')
    async def lastreact(self, ctx):
        await ctx.send_help(ctx.command)

    @lastreact.command(name='enable', brief='Enable auto nope react')
    @commands.has_role('Prabh')
    async def enable_lastreact(self, ctx):
        self.guild_map[ctx.guild.id].auto_nope_react = True
        await ctx.send(embed=discord_common.embed_success('Enabled auto nope react'))

    @lastreact.command(name='disable', brief='Disable auto nope react')
    @commands.has_role('Prabh')
    async def disable_lastreact(self, ctx):
        self.guild_map[ctx.guild.id].auto_nope_react = False
        await ctx.send(embed=discord_common.embed_success('Disabled auto nope react'))

    #  Self First React Command Group
    @commands.group(brief="Manage reactions for self first react`", invoke_without_command=True)
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def firstreact(self, ctx):
        await ctx.send_help(ctx.command)

    @firstreact.command(name='enable', brief='Enable self first react')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def enable_firstreact(self, ctx):
        self.guild_map[ctx.guild.id].add_first_reaction = True
        await ctx.send(embed=discord_common.embed_success('Enabled self first react'))

    @firstreact.command(name='disable', brief='Disable self first react')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def disable_firstreact(self, ctx):
        self.guild_map[ctx.guild.id].add_first_reaction = False
        await ctx.send(embed=discord_common.embed_success('Disabled self first react'))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None:
            return
        settings = self.guild_map[message.guild.id]
        if message.channel.id != settings.remind_channel_id or not message.embeds:
            return

        remind_role = self.bot.get_guild(message.guild.id).get_role(settings.remind_role_id)
        if settings.add_first_reaction and remind_role in message.role_mentions:
            await message.add_reaction(self.reaction_emoji)

        if settings.auto_nope_react:
            _, start_time = self.get_values_from_embed(message.embeds[0])
            delay = start_time - dt.datetime.utcnow().timestamp() + 300
            await asyncio.sleep(delay)
            message = await self.bot.get_channel(message.channel.id).fetch_message(message.id)
            if not message.reactions:
                await message.add_reaction(self.bot.get_emoji(self.nope_emoji))

    @commands.group(brief="Manage Final Call Reminder", invoke_without_command=True)
    async def final(self, ctx):
        await ctx.send_help(ctx.command)

    @final.command(name='here', brief='Set channel for final call')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_finalcall_settings(self, ctx, before: int):
        if not before or before < 0:
            raise RemindersCogError('Please provide valid `before` values')

        self.guild_map[ctx.guild.id].finalcall_before = before
        self.guild_map[ctx.guild.id].finalcall_channel_id = ctx.channel.id

        finalcall_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].finalcall_channel_id)

        embed = discord_common.embed_success('Final Call Settings Saved Successfully')
        embed.add_field(name='Final Call channel', value=finalcall_channel.mention)
        embed.add_field(name='Final Call Before',
                        value=f"{self.guild_map[ctx.guild.id].finalcall_before} mins before contest")

        await ctx.send(embed=embed)

    @commands.command(brief='Get Info about guild', invoke_without_command=True)
    async def settings(self, ctx):
        """Shows the current settings for the guild"""
        settings = self.guild_map[ctx.guild.id]

        remind_channel = ctx.guild.get_channel(settings.remind_channel_id)
        remind_role = ctx.guild.get_role(settings.remind_role_id)

        finalcall_channel = ctx.guild.get_channel(settings.finalcall_channel_id)

        subscribed_websites = []
        for website, data in settings.website_patterns.items():
            if data.allowed_patterns:
                subscribed_websites.append(website)
        subscribed_websites_str = ", ".join(subscribed_websites)

        remind_before_str = "Not Set"
        final_before_str = "Not Set"
        if settings.remind_before is not None:
            remind_before_str = f"At {', '.join(str(before_mins) for before_mins in settings.remind_before)}" \
                                f" mins before contest"
        if settings.finalcall_before is not None:
            final_before_str = f"At {settings.finalcall_before} mins before contest"
        embed = discord_common.embed_success(f'Current settings')

        if remind_channel is not None:
            embed.add_field(name='Remind Channel', value=remind_channel.mention)
        else:
            embed.add_field(name='Remind Channel', value="Not Set")

        if remind_role is not None:
            embed.add_field(name='Remind Role', value=remind_role.mention)
        else:
            embed.add_field(name='Remind Role', value="Not Set")

        embed.add_field(name='Remind Before', value=remind_before_str)

        if finalcall_channel is not None:
            embed.add_field(name='Final Call Channel', value=finalcall_channel.mention)
        else:
            embed.add_field(name='Final Call Channel', value="Not Set")

        embed.add_field(name='Final Call Before', value=final_before_str)
        embed.add_field(name="\u200b", value="\u200b")

        embed.add_field(name='Subscribed websites', value=f'{subscribed_websites_str}', inline=False)

        embed.set_footer(text=ctx.guild.name, icon_url=ctx.guild.icon)
        await ctx.send(embed=embed)

    @discord_common.send_error_if(RemindersCogError)
    async def cog_command_error(self, ctx, error):
        pass


async def setup(bot):
    await bot.add_cog(Reminders(bot))
