import asyncio
import random
import json
import pickle
import logging
import time
import datetime as dt
from pathlib import Path
import pytz
import copy

from collections import defaultdict
from recordtype import recordtype

import discord
from discord.ext import commands

from remind.util.rounds import Round
from remind.util import discord_common
from remind.util import paginator
from remind import constants
from remind.util import clist_api as clist


class RemindersCogError(commands.CommandError):
    pass


_CONTESTS_PER_PAGE = 5
_CONTEST_PAGINATE_WAIT_TIME = 5 * 60
_FINISHED_CONTESTS_LIMIT = 5
_CONTEST_REFRESH_PERIOD = 10 * 60  # seconds
_GUILD_SETTINGS_BACKUP_PERIOD = 6 * 60 * 60  # seconds

_PYTZ_TIMEZONES_GIST_URL = "https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568"

GuildSettings = recordtype(
    'GuildSettings', [
        ('remind_channel_id', None), ('remind_role_id', None), ('remind_before', None),
        ('finalcall_channel_id', None), ('finalcall_before', None),
        ('localtimezone', pytz.timezone("UTC")),
        ('website_allowed_patterns', defaultdict(list)),
        ('website_disallowed_patterns', defaultdict(list))])

_WEBSITE_DISALLOWED_PATTERNS = defaultdict(list)
_WEBSITE_DISALLOWED_PATTERNS['codeforces.com'] = ['wild', 'fools', 'kotlin', 'unrated']
_WEBSITE_DISALLOWED_PATTERNS['codechef.com'] = ['unrated', 'long']
_WEBSITE_DISALLOWED_PATTERNS['atcoder.jp'] = []
_WEBSITE_DISALLOWED_PATTERNS['codingcompetitions.withgoogle.com'] = ['registration', 'coding practice']
_WEBSITE_DISALLOWED_PATTERNS['facebook.com/hackercup'] = []
_WEBSITE_DISALLOWED_PATTERNS['leetcode.com'] = []

_WEBSITE_ALLOWED_PATTERNS = defaultdict(list)
_WEBSITE_ALLOWED_PATTERNS['codeforces.com'] = ['']
_WEBSITE_ALLOWED_PATTERNS['codechef.com'] = ['lunch', 'cook', 'rated']
_WEBSITE_ALLOWED_PATTERNS['atcoder.jp'] = ['abc:', 'arc:', 'agc:', 'grand', 'beginner', 'regular']
_WEBSITE_ALLOWED_PATTERNS['codingcompetitions.withgoogle.com'] = ['']
_WEBSITE_ALLOWED_PATTERNS['facebook.com/hackercup'] = ['']
_WEBSITE_ALLOWED_PATTERNS['usaco.org'] = ['']
_WEBSITE_ALLOWED_PATTERNS['leetcode.com'] = ['']

_SUPPORTED_WEBSITES = {
    "cf": "codeforces.com",
    "codeforces": "codeforces.com",
    "cc": "codechef.com",
    "codechef": "codechef.com",
    "atc": "atcoder.jp",
    "atcoder": "atcoder.jp",
    "google": "codingcompetitions.withgoogle.com",
    "hackercup": "facebook.com/hackercup",
    "leetcode": "leetcode.com",
    "usaco": "usaco.org",
}


class RemindRequest:
    def __init__(self, channel, role, contests, before_secs, send_time, localtimezone, scheduled, final_call):
        self.channel = channel
        self.role = role
        self.contests = contests
        self.before_secs = before_secs
        self.send_time = send_time
        self.localtimezone = localtimezone
        self.scheduled = scheduled
        self.final_call = final_call

    def schedule_remind(self):
        self.scheduled = True

    def unschedule_remind(self):
        self.scheduled = False


def get_default_guild_settings():
    allowed_patterns = copy.deepcopy(_WEBSITE_ALLOWED_PATTERNS)
    disallowed_patterns = copy.deepcopy(_WEBSITE_DISALLOWED_PATTERNS)
    settings = GuildSettings()
    settings.website_allowed_patterns = allowed_patterns
    settings.website_disallowed_patterns = disallowed_patterns
    return settings


def _contest_start_time_format(contest, tz):
    start = contest.start_time.replace(tzinfo=dt.timezone.utc).astimezone(tz)
    return f'{start.strftime("%d %b %y, %H:%M")} {tz}'


def _contest_duration_format(contest):
    duration_days, duration_hrs, duration_mins, _ = discord_common.time_format(contest.duration.total_seconds())
    duration = f'{duration_hrs}h {duration_mins}m'
    if duration_days > 0:
        duration = f'{duration_days}d ' + duration
    return duration


def _get_formatted_contest_desc(start, duration, url, max_duration_len):
    em = '\N{EN SPACE}'
    sq = '\N{WHITE SQUARE WITH UPPER RIGHT QUADRANT}'
    desc = (f'`{em}{start}{em}|{em}{duration.rjust(max_duration_len, em)}{em}|'
            f'{em}`[`link {sq}`]({url} "Link to contest page")')
    return desc


def _contest_website(contest):
    website = "Not Found"
    url = str(contest.url)
    if url.find("codeforces") != -1:
        website = "CodeForces"
    elif url.find("codechef") != -1:
        website = "CodeChef"
    elif url.find("atcoder") != -1:
        website = "AtCoder"
    elif url.find("google") != -1:
        website = "Google"
    elif url.find("hacker-cup") != -1:
        website = "FB HackerCup"
    elif url.find("leetcode") != -1:
        website = "LeetCode"
    elif url.find("usaco") != -1:
        website = "USACO"
    return website


def _get_embed_fields_from_contests(contests, localtimezone):
    infos = [(contest.name,
              _contest_start_time_format(contest, localtimezone),
              _contest_website(contest),
              _contest_duration_format(contest),
              contest.url) for contest in contests]
    max_duration_len = max(len(duration) for _, _, _, duration, _ in infos)

    fields = []
    for name, start, website, duration, url in infos:
        value = _get_formatted_contest_desc(start, duration, url, max_duration_len)
        fields.append((name, value, website))
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

    # if request.final_call:

    labels = 'day hr min sec'.split()
    before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
    desc = f'About to start in {before_str}'
    embed = discord_common.color_embed(description=desc)
    for name, value, website in _get_embed_fields_from_contests(request.contests, request.localtimezone):
        embed.add_field(name=(website + " || " + name), value=value)
    await request.channel.send(request.role.mention, embed=embed)


def filter_contests(filters, contests):
    if not filters:
        return contests

    filtered_contests = []
    for contest in contests:
        eligible = False
        for contest_filter in filters:
            if contest_filter[0] == "+" and contest_filter[1:] in _SUPPORTED_WEBSITES.keys():
                website = _SUPPORTED_WEBSITES[contest_filter[1:]]
                eligible |= contest.is_eligible(website)
        if eligible:
            filtered_contests.append(contest)
    return filtered_contests


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

        self.finalcall_tasks_map = defaultdict(lambda: defaultdict(int))
        self.finalcall_subscribe_count = defaultdict(int)

        self.member_converter = commands.MemberConverter()
        self.role_converter = commands.RoleConverter()

        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        guild_map_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        try:
            with guild_map_path.open('rb') as guild_map_file:
                guild_map = pickle.load(guild_map_file)
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
        self._reschedule_tasks(ctx.guild.id)

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
        self.contest_cache = [contest for contest in contests if contest.is_desired(_WEBSITE_ALLOWED_PATTERNS,
                                                                                    _WEBSITE_DISALLOWED_PATTERNS)]

    def get_guild_contests(self, contests, guild_id):
        settings = self.guild_map[guild_id]

        desired_contests = []
        for contest in contests:
            if contest.is_desired(settings.website_allowed_patterns, settings.website_disallowed_patterns):
                desired_contests.append(contest)

        return desired_contests

    def _reschedule_all_tasks(self):
        for guild in self.bot.guilds:
            self._reschedule_tasks(guild.id)

    def _reschedule_tasks(self, guild_id):
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
            for before_mins in settings.remind_before:
                before_secs = 60 * before_mins
                request = RemindRequest(channel, role, contests, before_secs, start_time - before_secs,
                                        settings.localtimezone, True, False)
                task = asyncio.create_task(_send_reminder_at(request))
                self.task_map[guild_id].append(task)

        self.logger.info(
            f'{len(self.task_map[guild_id])} tasks scheduled for guild "{self.bot.get_guild(guild_id)}"')

    @staticmethod
    def _make_contest_pages(contests, title, localtimezone):
        pages = []
        chunks = paginator.chunkify(contests, _CONTESTS_PER_PAGE)
        for chunk in chunks:
            embed = discord_common.color_embed()
            for name, value, website in _get_embed_fields_from_contests(chunk, localtimezone):
                embed.add_field(name=(website + " || " + name), value=value, inline=False)
            pages.append((title, embed))
        return pages

    async def _send_contest_list(self, ctx, contests, *, title, empty_msg):
        if contests is None:
            raise RemindersCogError('Contest list not present')
        if len(contests) == 0:
            await ctx.send(embed=discord_common.embed_neutral(empty_msg))
            return
        pages = self._make_contest_pages(contests, title, self.guild_map[ctx.guild.id].localtimezone)
        paginator.paginate(self.bot, ctx.channel, pages,
                           wait_time=_CONTEST_PAGINATE_WAIT_TIME,
                           set_pagenum_footers=True)

    def _serialize_guild_map(self):
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        with out_path.open(mode='wb') as out_file:
            pickle.dump(self.guild_map, out_file)

    def _backup_serialize_guild_map(self):
        current_time_stamp = int(dt.datetime.utcnow().timestamp())
        if current_time_stamp - self.last_guild_backup_time < _GUILD_SETTINGS_BACKUP_PERIOD:
            return

        self.last_guild_backup_time = current_time_stamp
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH + "_" + str(current_time_stamp))
        with out_path.open(mode='wb') as out_file:
            pickle.dump(self.guild_map, out_file)

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

    @remind.command(brief='Resets the judges settings to the default ones')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def reset_judges_settings(self, ctx):
        """ Resets the judges settings to the default ones.
        """
        default_settings = get_default_guild_settings()
        self.guild_map[ctx.guild.id].website_allowed_patterns = default_settings.default_allowed_patterns
        self.guild_map[ctx.guild.id].website_disallowed_patterns = default_settings.default_disallowed_patterns
        await ctx.send(embed=discord_common.embed_success('Succesfully reset the judges settings to the default ones'))

    # @remind.command(brief='Show reminder settings')
    # async def settings(self, ctx):
    #     """Shows the current settings for the guild"""
    #     settings = self.guild_map[ctx.guild.id]
    #
    #     remind_channel = ctx.guild.get_channel(settings.remind_channel_id)
    #     remind_role = ctx.guild.get_role(settings.remind_role_id)
    #
    #     finalcall_channel = ctx.guild.get_channel(settings.finalcall_channel_id)
    #
    #     subscribed_websites_str = ", ".join(
    #         website for website, patterns in settings.website_allowed_patterns.items() if patterns)
    #
    #     remind_before_str = "Not Set"
    #     final_before_str = "Not Set"
    #     if settings.remind_before is not None:
    #         remind_before_str = f"At {', '.join(str(before_mins) for before_mins in settings.remind_before)}" \
    #                             f" mins before contest"
    #     if settings.finalcall_before is not None:
    #         final_before_str = f"At {settings.finalcall_before} mins before contest"
    #     embed = discord_common.embed_success('Current reminder settings')
    #
    #     if remind_channel is not None:
    #         embed.add_field(name='Remind Channel', value=remind_channel.mention)
    #     else:
    #         embed.add_field(name='Remind Channel', value="Not Set")
    #
    #     if remind_role is not None:
    #         embed.add_field(name='Remind Role', value=remind_role.mention)
    #     else:
    #         embed.add_field(name='Remind Role', value="Not Set")
    #
    #     embed.add_field(name='Remind Before', value=remind_before_str)
    #
    #     if finalcall_channel is not None:
    #         embed.add_field(name='Final Call Channel', value=finalcall_channel.mention)
    #     else:
    #         embed.add_field(name='Final Call Channel', value="Not Set")
    #
    #     embed.add_field(name='Final Call Before', value=final_before_str)
    #
    #     embed.add_field(name='Subscribed websites', value=f'{subscribed_websites_str}')
    #     await ctx.send(embed=embed)

    def _get_remind_role(self, guild):
        settings = self.guild_map[guild.id]
        role_id = settings.remind_role_id
        if role_id is None:
            raise RemindersCogError('No role set for reminders')
        role = guild.get_role(role_id)
        if role is None:
            raise RemindersCogError('The role set for reminders is no longer available.')
        return role

    # @remind.command(brief='Subscribe to contest reminders')
    # async def on(self, ctx):
    #     """Subscribes you to contest reminders.
    #     Use 't;remind settings' to see the current settings.
    #     """
    #     role = self._get_remind_role(ctx.guild)
    #     if role in ctx.author.roles:
    #         embed = discord_common.embed_neutral('You are already subscribed to contest reminders')
    #     else:
    #         await ctx.author.add_roles(role, reason='User subscribed to contest reminders')
    #         embed = discord_common.embed_success('Successfully subscribed to contest reminders')
    #     await ctx.send(embed=embed)
    #
    # @remind.command(brief='Unsubscribe from contest reminders')
    # async def off(self, ctx):
    #     """Unsubscribes you from contest reminders."""
    #     role = self._get_remind_role(ctx.guild)
    #     if role not in ctx.author.roles:
    #         embed = discord_common.embed_neutral('You are not subscribed to contest reminders')
    #     else:
    #         await ctx.author.remove_roles(role, reason='User unsubscribed from contest reminders')
    #         embed = discord_common.embed_success('Successfully unsubscribed from contest reminders')
    #     await ctx.send(embed=embed)

    def _set_guild_setting(self, guild_id, websites, allowed_patterns, disallowed_patterns):

        guild_settings = self.guild_map[guild_id]
        _ALLOWED_WEBSITES = set(_SUPPORTED_WEBSITES.values())
        supported_websites, unsupported_websites = [], []
        for website in websites:
            if website not in _ALLOWED_WEBSITES:
                unsupported_websites.append(website)
                continue

            guild_settings.website_allowed_patterns[website] = allowed_patterns[website]
            guild_settings.website_disallowed_patterns[website] = disallowed_patterns[website]
            supported_websites.append(website)

        self.guild_map[guild_id] = guild_settings
        return supported_websites, unsupported_websites

    @remind.command(brief='Start contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def subscribe(self, ctx, *websites: str):
        """Start contest reminders from websites."""

        _ALLOWED_WEBSITES = set(_SUPPORTED_WEBSITES.values())
        if all(website not in _ALLOWED_WEBSITES for website in websites):
            supported_websites = ", ".join(_ALLOWED_WEBSITES)
            embed = discord_common.embed_alert(
                f'None of these websites are supported for contest reminders.'
                f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            subscribed, unsupported = self._set_guild_setting(guild_id, websites, _WEBSITE_ALLOWED_PATTERNS,
                                                              _WEBSITE_DISALLOWED_PATTERNS)
            subscribed_websites_str = ", ".join(subscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully subscribed from  {subscribed_websites_str} for contest reminders.'
            success_str += f'\n{unsupported_websites_str} \
                {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Stop contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def unsubscribe(self, ctx, *websites: str):
        """Stop contest reminders from websites."""

        _ALLOWED_WEBSITES = set(_SUPPORTED_WEBSITES.values())
        if all(website not in _ALLOWED_WEBSITES for website in websites):
            supported_websites = ", ".join(_ALLOWED_WEBSITES)
            embed = discord_common.embed_alert(f'None of these websites are supported for contest reminders.'
                                               f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            unsubscribed, unsupported = self._set_guild_setting(guild_id, websites, defaultdict(list),
                                                                defaultdict(lambda: ['']))
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

    @commands.command(brief='Set the server\'s timezone', usage=' <timezone>')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def settz(self, ctx, timezone: str = None):
        """ Sets the server's timezone to the given timezone. """

        if not (timezone in pytz.all_timezones):
            desc = ('The given timezone is absent/invalid\n\n'
                    'Examples of valid timezones:\n\n')
            desc += '\n'.join(random.sample(pytz.all_timezones, 5))
            desc += '\n\nAll valid timezones can be found [here]'
            desc += f'({_PYTZ_TIMEZONES_GIST_URL})'
            raise RemindersCogError(desc)

        self.guild_map[ctx.guild.id].localtimezone = pytz.timezone(timezone)
        await ctx.send(embed=discord_common.embed_success(f'Succesfully set the server timezone to {timezone}'))

    @commands.group(brief='Commands for listing contests', invoke_without_command=True)
    async def clist(self, ctx):
        """
        Show past, present and future contests.Use filters to get contests from specific website

        Supported Filters : +cf/+codeforces +atc/+atcoder +cc/+codechef +hackercup +google +usaco +leetcode

        Eg: t;clist future +atc +codeforces
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

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        reaction_emoji = "✅"
        settings = self.guild_map[payload.guild_id]
        if settings.remind_channel_id is None or settings.remind_channel_id != payload.channel_id \
                or payload.emoji.name != reaction_emoji or settings.finalcall_channel_id is None:
            return

        member_dm = await payload.member.create_dm()
        await member_dm.send("Final Call alarm set Successfully")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        reaction_emoji = "✅"
        settings = self.guild_map[payload.guild_id]
        if settings.remind_channel_id is None or settings.remind_channel_id != payload.channel_id \
                or payload.emoji.name != reaction_emoji or settings.finalcall_channel_id is None:
            return

        member = self.bot.get_user(payload.user_id)
        member_dm = await member.create_dm()
        await member_dm.send("Final Call alarm removed Successfully")

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
                        value=f"At {self.guild_map[ctx.guild.id].finalcall_before} mins before contest")

        await ctx.send(embed=embed)

    @commands.command(brief='Get Info about guild',invoke_without_command=True)
    async def settings(self, ctx):
        """Shows the current settings for the guild"""
        settings = self.guild_map[ctx.guild.id]

        remind_channel = ctx.guild.get_channel(settings.remind_channel_id)
        remind_role = ctx.guild.get_role(settings.remind_role_id)

        finalcall_channel = ctx.guild.get_channel(settings.finalcall_channel_id)

        subscribed_websites_str = ", ".join(
            website for website, patterns in settings.website_allowed_patterns.items() if patterns)

        remind_before_str = "Not Set"
        final_before_str = "Not Set"
        if settings.remind_before is not None:
            remind_before_str = f"At {', '.join(str(before_mins) for before_mins in settings.remind_before)}" \
                                f" mins before contest"
        if settings.finalcall_before is not None:
            final_before_str = f"At {settings.finalcall_before} mins before contest"
        embed = discord_common.embed_success(f'Current settings for {ctx.guild.name}')

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

        embed.add_field(name='Subscribed websites', value=f'{subscribed_websites_str}')
        await ctx.send(embed=embed)


    @discord_common.send_error_if(RemindersCogError)
    async def cog_command_error(self, ctx, error):
        pass


def setup(bot):
    bot.add_cog(Reminders(bot))
