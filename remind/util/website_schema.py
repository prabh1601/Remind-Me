from collections import defaultdict


class WebsitePatterns:
    def __init__(self, *, _allowed_patterns=None, _disallowed_patterns=None, _shorthands=None, _prefix="",
                 _show_prefix=True):
        if _shorthands is None:
            _shorthands = []
        if _disallowed_patterns is None:
            _disallowed_patterns = []
        if _allowed_patterns is None:
            _allowed_patterns = []

        self.allowed_patterns = _allowed_patterns
        self.disallowed_patterns = _disallowed_patterns
        self.shorthands = _shorthands
        self.prefix = _prefix
        self.show_prefix = _show_prefix


schema = defaultdict(WebsitePatterns)

schema['codeforces.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=['wild', 'fools', 'kotlin', 'unrated'],
    _shorthands=['cf', 'codeforces'],
    _prefix='CF',
    _show_prefix=False,
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
    _show_prefix=False,
)

schema['codingcompetitions.withgoogle.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=['registration', 'coding practice'],
    _shorthands=['google'],
    _prefix='Google'
)

schema['usaco.org'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['usaco'],
    _prefix='USACO'
)

schema['facebook.com/hackercup'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['hackercup', 'fbhc'],
    _prefix='Meta Hackercup'
)

schema['leetcode.com'] = WebsitePatterns(
    _allowed_patterns=[''],
    _disallowed_patterns=[],
    _shorthands=['leetcode', 'lc'],
    _prefix='LeetCode'
)
