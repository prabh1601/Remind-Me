from collections import defaultdict
import re


class WebsitePatterns:
    def __init__(self, *,
                 _allowed_patterns=None,
                 _disallowed_patterns=None,
                 _shorthands=None,
                 _prefix="",
                 _normalize_regex=".*",
                 _rare=False):

        self.allowed_patterns = _allowed_patterns or []
        self.disallowed_patterns = _disallowed_patterns or []
        self.shorthands = _shorthands or []
        self.prefix = _prefix
        self._normalize_regex = _normalize_regex
        self.rare = _rare

    def normalize(self, name):
        try:
            name = re.compile(self._normalize_regex).search(name).group()
        except AttributeError:
            pass
        return name


# Todo : Move this to external db
schema = defaultdict(WebsitePatterns)

schema['codeforces.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=['wild', 'fools', 'kotlin', 'unrated'],
    _shorthands=['cf', 'codeforces'],
    _prefix='CodeForces',
)

schema['codechef.com'] = WebsitePatterns(
    _allowed_patterns=['lunch', 'cook', 'starters', 'rated'],
    _disallowed_patterns=['unrated', 'long'],
    _shorthands=['cc', 'codechef'],
    _prefix='CodeChef',
)

schema['atcoder.jp'] = WebsitePatterns(
    _allowed_patterns=['abc:', 'arc:', 'agc:', 'grand', 'beginner', 'regular'],
    _disallowed_patterns=[],
    _shorthands=['ac', 'atcoder'],
    _prefix='AtCoder',
    _normalize_regex="AtCoder .* Contest [0-9]+"
)

schema['codingcompetitions.withgoogle.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=['registration', 'coding practice'],
    _shorthands=['google'],
    _prefix='Google',
    _rare=True
)

schema['usaco.org'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['usaco'],
    _prefix='USACO',
    _rare=True
)

schema['facebook.com/hackercup'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['hackercup', 'fbhc'],
    _prefix='Meta Hackercup',
    _rare=True
)

schema['leetcode.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['leetcode', 'lc'],
    _prefix='LeetCode'
)
