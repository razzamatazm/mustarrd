import re
from datetime import datetime
from typing import Optional, Tuple


class FileNamer:
    # Sports keywords for detection
    SPORTS_KEYWORDS = [
        'vs', 'vs.', ' @ ', 'nfl', 'nba', 'nhl', 'mlb', 'ufc', 'boxing',
        'soccer', 'football', 'basketball', 'hockey', 'baseball', 'tennis',
        'golf', 'racing', 'motorsport', 'wrestling', 'mma', 'fifa', 'uefa',
        'premier league', 'la liga', 'serie a', 'bundesliga', 'champions league'
    ]

    SPORTS_CATEGORIES = ['sports', 'deportes', 'sport', 'esports']

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Remove invalid characters for filesystem."""
        # Remove stylized "New" markers used in some EPG titles
        name = re.sub(r'[\[\(\-–—|:]*\s*ᴺᵉʷ\s*[\]\)]*\s*(?:-\s*)?', ' ', name)
        # Remove stylized "Live" markers frequently injected by providers
        name = re.sub(r'[\[\(\-–—|:]*\s*ᴸᶦᵛᵉ\s*[\]\)]*\s*(?:-\s*)?', ' ', name)

        # Replace invalid characters with spaces (includes null byte and other control chars)
        invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
        sanitized = re.sub(invalid_chars, ' ', name)
        # Remove multiple spaces
        sanitized = re.sub(r'\s+', ' ', sanitized)
        # Remove leading/trailing spaces and dots
        sanitized = sanitized.strip(' .')
        # Limit length
        if len(sanitized) > 200:
            sanitized = sanitized[:200]
        return sanitized

    @staticmethod
    def extract_season_episode(text: str) -> Optional[Tuple[int, int]]:
        """Extract season and episode numbers from text."""
        # Common patterns: S01E01, S1E1, Season 1 Episode 1, 1x01
        patterns = [
            r'[Ss](\d{1,2})[Ee](\d{1,2})',  # S01E01
            r'[Ss]eason\s*(\d{1,2})\s*[Ee]pisode\s*(\d{1,2})',  # Season 1 Episode 1
            r'(\d{1,2})[xX](\d{1,2})',  # 1x01
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return (int(match.group(1)), int(match.group(2)))

        return None

    @staticmethod
    def extract_show_name(title: str) -> str:
        """Extract the show name from a title with season/episode info."""
        # Remove season/episode patterns
        patterns = [
            r'\s*[Ss]\d{1,2}[Ee]\d{1,2}.*$',
            r'\s*[Ss]eason\s*\d+.*$',
            r'\s*\d{1,2}[xX]\d{1,2}.*$',
        ]

        show_name = title
        for pattern in patterns:
            show_name = re.sub(pattern, '', show_name, flags=re.IGNORECASE)

        return show_name.strip(' -:')

    @staticmethod
    def extract_episode_title(title: str) -> str:
        """Extract the episode title from a full title."""
        # Try to find content after season/episode pattern
        patterns = [
            r'[Ss]\d{1,2}[Ee]\d{1,2}\s*[-:]\s*(.+)$',
            r'[Ss]eason\s*\d+\s*[Ee]pisode\s*\d+\s*[-:]\s*(.+)$',
            r'\d{1,2}[xX]\d{1,2}\s*[-:]\s*(.+)$',
        ]

        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return match.group(1).strip()

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
        title_lower = title.lower()
        category_lower = category.lower()

        return (
            any(kw in title_lower for kw in cls.SPORTS_KEYWORDS) or
            any(cat in category_lower for cat in cls.SPORTS_CATEGORIES)
        )

    def generate_filename(
        self,
        program: dict,
        channel: dict,
        program_type: str,
        settings: dict = None
    ) -> str:
        """
        Generate a smart filename based on content type.

        Args:
            program: Program data with title, description, start_time, etc.
            channel: Channel data with name, category_name, etc.
            program_type: Detected type ('tv_show', 'movie', 'sports', 'other')
            settings: App settings with naming templates

        Returns:
            Sanitized filename with .ts extension
        """
        title = program.get("title", "Unknown")
        description = program.get("description", "")
        start_time_str = program.get("start_time")

        # Parse start time
        if start_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str)
            except ValueError:
                start_time = datetime.utcnow()
        else:
            start_time = datetime.utcnow()

        date_str = start_time.strftime("%Y-%m-%d")

        # Generate filename based on type
        if program_type == "tv_show":
            filename = self._generate_tv_filename(title, description, date_str)
        elif program_type == "sports":
            filename = self._generate_sports_filename(title, date_str)
        elif program_type == "movie":
            filename = self._generate_movie_filename(title, description, start_time)
        else:
            filename = self._generate_default_filename(title, date_str)

        return self.sanitize_filename(filename) + ".ts"

    def _generate_tv_filename(self, title: str, description: str, date_str: str) -> str:
        """Generate filename for TV shows."""
        full_text = f"{title} {description}"
        season_ep = self.extract_season_episode(full_text)

        if season_ep:
            show_name = self.extract_show_name(title)
            episode_title = self.extract_episode_title(title)

            if episode_title:
                return f"{show_name} - S{season_ep[0]:02d}E{season_ep[1]:02d} - {episode_title}"
            else:
                return f"{show_name} - S{season_ep[0]:02d}E{season_ep[1]:02d}"
        else:
            # No season/episode found, use default format
            return f"{title} - {date_str}"

    def _generate_sports_filename(self, title: str, date_str: str) -> str:
        """Generate filename for sports content."""
        return f"{title} - {date_str}"

    def _generate_movie_filename(self, title: str, description: str, start_time: datetime) -> str:
        """Generate filename for movies."""
        year = self.extract_year(description) or self.extract_year(title) or start_time.year

        # Clean title of year if present
        clean_title = re.sub(r'\s*\(\d{4}\)\s*', '', title).strip()

        return f"{clean_title} ({year})"

    def _generate_default_filename(self, title: str, date_str: str) -> str:
        """Generate default filename."""
        return f"{title} - {date_str}"


# Global instance
file_namer = FileNamer()
