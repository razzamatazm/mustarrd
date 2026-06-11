import re
from datetime import datetime
from typing import Optional, Tuple


class FileNamer:
    # Shared season/episode patterns. Order matters: the S/E form is tried first
    # so "S54 E173" doesn't fall through to the looser NxNN form.
    # Covers: S01E01, S54 E173, S54.E173, S54-E173, S04, E12,
    # Season 4 Episode 12, Season 4, Episode 12, S54 Ep. 173, Sn 4 Ep 12
    SEASON_EPISODE_PATTERNS = [
        re.compile(
            r'\b[Ss](?:eason|n)?\s*(\d{1,2})\s*[.,\-]?\s*'
            r'[Ee](?:p(?:isode)?)?\.?\s*(\d{1,4})\b'
        ),
        # 4x12, 54x173 — no spaces around the x, so "3 x 4" in prose doesn't match
        re.compile(r'\b(\d{1,2})[xX](\d{1,4})\b'),
    ]

    # Word-boundary sports detection: bare substrings like 'vs' or 'mma'
    # false-positive inside words ("canvas", "grammar").
    SPORTS_TITLE_PATTERN = re.compile(
        r'\bvs\.?\b'
        r'|@'
        r'|\b(?:nfl|nba|wnba|nhl|mlb|mls|ufc|mma|ncaa|fifa|uefa|f1|nascar|motogp|pga|lpga|atp|wta)\b'
        r'|\b(?:boxing|soccer|football|basketball|hockey|baseball|tennis|golf|racing|motorsport|wrestling|cricket|rugby)\b'
        r'|\b(?:premier league|la liga|serie a|bundesliga|ligue 1|champions league|europa league'
        r'|world cup|stanley cup|super bowl|world series|grand prix)\b'
        r'|\b(?:round|week|matchday)\s+\d+\b'
        r'|\bplayoffs?\b|\bsemi-?finals?\b|\bquarter-?finals?\b',
        re.IGNORECASE,
    )

    SPORTS_CATEGORIES = ['sports', 'deportes', 'sport', 'esports']

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Remove invalid characters for filesystem."""
        # Remove stylized "New" markers used in some EPG titles
        name = re.sub(r'[\[\(\-–—|:]*\s*ᴺᵉʷ\s*[\]\)]*\s*(?:-\s*)?', ' ', name)
        # Remove stylized "Live" markers frequently injected by providers
        name = re.sub(r'[\[\(\-–—|:]*\s*ᴸᶦᵛᵉ\s*[\]\)]*\s*(?:-\s*)?', ' ', name)

        # Remove invisible Unicode format/directional chars: zero-width, BOM, soft hyphen,
        # and bidi override/isolate controls (U+202A-202E, U+2066-2069) that can make
        # filenames display reversed in terminals and file managers.
        name = re.sub('[\u00ad\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff]', '', name)

        # Replace invalid filesystem chars with spaces (includes null byte and control chars)
        invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
        sanitized = re.sub(invalid_chars, ' ', name)
        # Remove multiple spaces
        sanitized = re.sub(r'\s+', ' ', sanitized)
        # Remove leading/trailing spaces and dots
        sanitized = sanitized.strip(' .') or "unknown-program"
        # Limit to 200 UTF-8 bytes so CJK/Arabic titles don't exceed Linux
        # NAME_MAX (255 bytes) when combined with an extension.
        encoded = sanitized.encode("utf-8")
        if len(encoded) > 200:
            sanitized = encoded[:200].decode("utf-8", errors="ignore").rstrip() or "unknown-program"
        return sanitized

    @classmethod
    def extract_season_episode(cls, text: str) -> Optional[Tuple[int, int]]:
        """Extract season and episode numbers from text."""
        for pattern in cls.SEASON_EPISODE_PATTERNS:
            match = pattern.search(text)
            if match:
                return (int(match.group(1)), int(match.group(2)))

        return None

    @classmethod
    def extract_show_name(cls, title: str) -> str:
        """Extract the show name from a title with season/episode info."""
        for pattern in cls.SEASON_EPISODE_PATTERNS:
            match = pattern.search(title)
            if match:
                before = title[:match.start()].strip(' -:,.(')
                if before:
                    return before
                # Pattern at the very start of the title — keep whatever follows
                return pattern.sub('', title, count=1).strip(' -:,.)')

        # Season-only titles like "Show Name Season 4"
        show_name = re.sub(r'\s*[Ss]eason\s*\d+.*$', '', title)
        return show_name.strip(' -:')

    @classmethod
    def extract_episode_title(cls, title: str) -> str:
        """Extract the episode title from a full title."""
        for pattern in cls.SEASON_EPISODE_PATTERNS:
            match = pattern.search(title)
            if match:
                after = title[match.end():]
                sep = re.match(r'\s*[-–:]\s*(.+)$', after)
                if sep:
                    return sep.group(1).strip()
                return ""

        return ""

    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        """Extract a year (1900-2099) from text."""
        match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
        if match:
            return int(match.group(1))
        return None

    @classmethod
    def detect_sports(cls, title: str, category: str = "") -> bool:
        """Check if content is sports-related."""
        category_lower = category.lower()

        return (
            bool(cls.SPORTS_TITLE_PATTERN.search(title)) or
            any(cat in category_lower for cat in cls.SPORTS_CATEGORIES)
        )

    _DEFAULT_TEMPLATES = {
        "tv": "{show} - S{season:02d}E{episode:02d} - {title}",
        "tv_no_subtitle": "{show} - S{season:02d}E{episode:02d}",
        "movie": "{title} ({year})",
        "sports": "{title} - {date}",
        "default": "{title} - {date}",
    }

    def generate_filename(
        self,
        program: dict,
        channel: dict,
        program_type: str,
        settings: dict = None
    ) -> str:
        title = program.get("title", "Unknown")
        description = program.get("description", "")
        start_time_str = program.get("start_time")
        channel_name = channel.get("name", "")

        if start_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str)
            except (ValueError, TypeError):
                start_time = datetime.utcnow()
        else:
            start_time = datetime.utcnow()

        date_str = start_time.strftime("%Y-%m-%d")
        s = settings or {}

        if program_type == "tv_show":
            full_text = f"{title} {description}"
            season_ep = self.extract_season_episode(full_text)
            if season_ep:
                show_name = self.extract_show_name(title)
                episode_title = self.extract_episode_title(title)
                context = {
                    "show": show_name, "season": season_ep[0], "episode": season_ep[1],
                    "title": episode_title, "date": date_str, "channel": channel_name,
                }
                custom = s.get("tv_template")
                if custom and episode_title:
                    template = custom
                elif episode_title:
                    template = self._DEFAULT_TEMPLATES["tv"]
                else:
                    template = self._DEFAULT_TEMPLATES["tv_no_subtitle"]
            else:
                context = {"title": title, "date": date_str, "channel": channel_name}
                template = s.get("default_template") or self._DEFAULT_TEMPLATES["default"]
        elif program_type == "sports":
            context = {"title": title, "date": date_str, "channel": channel_name}
            template = s.get("sports_template") or self._DEFAULT_TEMPLATES["sports"]
        elif program_type == "movie":
            year = self.extract_year(description) or self.extract_year(title) or start_time.year
            clean_title = re.sub(r'\s*\(\d{4}\)\s*', '', title).strip()
            context = {"title": clean_title, "year": year, "date": date_str, "channel": channel_name}
            template = s.get("movie_template") or self._DEFAULT_TEMPLATES["movie"]
        else:
            context = {"title": title, "date": date_str, "channel": channel_name}
            template = s.get("default_template") or self._DEFAULT_TEMPLATES["default"]

        try:
            filename = template.format_map(context)
        except (KeyError, ValueError, AttributeError):
            filename = f"{title} - {date_str}"

        return self.sanitize_filename(filename) + ".ts"


# Global instance
file_namer = FileNamer()
