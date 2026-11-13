"""Poster lookup (#443): which poster a recording gets, and from whom.

Driven through ``FakeJsonClient``, so every test states what TMDB or TVMaze
would answer and asserts on what was asked and what came back. Nothing here
touches the network, the database or the disk.
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.poster_lookup import (  # noqa: E402
    KIND_MOVIE,
    KIND_TV,
    KIND_UNKNOWN,
    SOURCE_TMDB,
    SOURCE_TVMAZE,
    TMDB_API_BASE,
    TMDB_IMAGE_BASE,
    TMDB_THUMBNAIL_BASE,
    TVMAZE_API_BASE,
    FakeJsonClient,
    PosterQuery,
    TmdbSource,
    TvMazeSource,
    build_sources,
    clean_lookup_title,
    normalize_title,
    query_from_download,
    resolve_poster,
)

# Stand-ins shaped like TMDB's two credentials, and obviously not real ones:
# secret scanners flag anything that looks like a hex key or a JWT.
V3_KEY = "example-short-tmdb-key"
V4_TOKEN = "eyJ" + "-example-long-tmdb-token" * 8


def _recording(title, **metadata):
    return SimpleNamespace(
        program_title=title,
        guide_metadata_json=json.dumps(metadata) if metadata else None,
    )


def _tmdb(path):
    return f"{TMDB_API_BASE}{path}"


def _tvmaze_show(name, image="https://static.tvmaze.com/uploads/images/original_untouched/1/1.jpg"):
    return {
        "name": name,
        "image": {
            "medium": image.replace("original_untouched", "medium_portrait"),
            "original": image,
        },
    }


class LookupTitleTests(unittest.TestCase):
    def test_guide_decorations_are_stripped_from_the_name(self):
        cases = {
            "Bluey": "Bluey",
            "Bluey S03E12 - Sleepytime": "Bluey",
            "New: Bluey": "Bluey",
            "Bluey (2018)": "Bluey",
            "Bluey (2018) [HD]": "Bluey",
            "LIVE - Match of the Day": "Match of the Day",
            "  ": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_lookup_title(raw), expected)

    def test_a_title_that_is_only_a_bracket_is_kept(self):
        self.assertEqual(clean_lookup_title("(500) Days of Summer"), "(500) Days of Summer")
        self.assertEqual(clean_lookup_title("[REC]"), "[REC]")

    def test_two_spellings_of_one_show_are_the_same_title(self):
        self.assertEqual(normalize_title("Tom & Jerry"), normalize_title("tom and jerry"))
        self.assertEqual(normalize_title("Pokémon"), normalize_title("POKEMON"))
        self.assertEqual(normalize_title("Mr. Robot"), normalize_title("Mr Robot"))

    def test_different_shows_stay_different(self):
        self.assertNotEqual(normalize_title("Bluey"), normalize_title("Bluey's Big Play"))

    def test_titles_in_other_alphabets_keep_their_letters(self):
        """Keeping only a-z turned "Новости 24" into "24", which matches the show 24."""
        self.assertNotEqual(normalize_title("Новости 24"), normalize_title("24"))
        self.assertNotEqual(normalize_title("Новости 24"), normalize_title("Вести 24"))
        self.assertNotEqual(normalize_title("MBC أخبار"), normalize_title("MBC"))
        self.assertEqual(normalize_title("НОВОСТИ 24"), normalize_title("новости 24"))
        self.assertTrue(normalize_title("Матрица"))


class QueryFromRecordingTests(unittest.TestCase):
    def test_a_numbered_episode_is_a_show(self):
        query = query_from_download(_recording("Detectorists", season=2, episode=4))

        self.assertEqual(query.title, "Detectorists")
        self.assertEqual(query.kind, KIND_TV)

    def test_numbering_in_the_title_also_makes_it_a_show(self):
        query = query_from_download(_recording("Detectorists S02E04 - The Hoard"))

        self.assertEqual(query.title, "Detectorists")
        self.assertEqual(query.kind, KIND_TV)

    def test_a_film_category_makes_it_a_film(self):
        query = query_from_download(_recording("Alien", categories=["Movie", "Sci-Fi"]))

        self.assertEqual(query.kind, KIND_MOVIE)

    def test_anything_else_is_unknown(self):
        self.assertEqual(query_from_download(_recording("News at Ten")).kind, KIND_UNKNOWN)

    def test_a_year_in_the_title_and_the_length_ride_along(self):
        recording = _recording("Alien (1979)")
        recording.duration_minutes = 123

        query = query_from_download(recording)

        self.assertEqual((query.title, query.year, query.runtime_minutes), ("Alien", 1979, 123))

    def test_guide_ids_ride_along(self):
        query = query_from_download(
            _recording("Detectorists", tvdb_id="281566", imdb_id="tt3630622", tmdb_id="61381")
        )

        self.assertEqual(
            (query.tvdb_id, query.imdb_id, query.tmdb_id), ("281566", "tt3630622", "61381")
        )

    def test_a_recording_with_nothing_to_go_on_has_no_query(self):
        self.assertIsNone(query_from_download(_recording("")))

    def test_unreadable_metadata_is_ignored_not_fatal(self):
        recording = SimpleNamespace(program_title="Bluey", guide_metadata_json="{not json")

        self.assertEqual(query_from_download(recording).title, "Bluey")


class TmdbTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_tmdb_id_for_a_known_show_is_fetched_directly(self):
        http = FakeJsonClient({_tmdb("/tv/1399"): (200, {"name": "Anything", "poster_path": "/got.jpg"})})

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Game of Thrones", kind=KIND_TV, tmdb_id="1399")
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/got.jpg",))
        self.assertEqual(match.thumbnail_url, f"{TMDB_THUMBNAIL_BASE}/got.jpg")
        self.assertEqual(match.source, SOURCE_TMDB)
        self.assertEqual(http.urls, [_tmdb("/tv/1399")], "An ID match needs no search.")

    async def test_a_prefixed_tmdb_id_says_which_kind_it_is(self):
        http = FakeJsonClient({_tmdb("/movie/603"): (200, {"title": "The Matrix", "poster_path": "/m.jpg"})})

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Whatever the guide called it", tmdb_id="movie/603")
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/m.jpg",))

    async def test_a_bare_tmdb_id_of_unknown_kind_must_agree_on_the_name(self):
        """TV 1399 and film 1399 are different things."""
        http = FakeJsonClient(
            {
                _tmdb("/tv/1399"): (200, {"name": "Game of Thrones", "poster_path": "/got.jpg"}),
                _tmdb("/movie/1399"): (200, {"title": "Some Film", "poster_path": "/film.jpg"}),
            }
        )

        wrong = await TmdbSource(V3_KEY, http).find(PosterQuery(title="News at Ten", tmdb_id="1399"))
        right = await TmdbSource(V3_KEY, http).find(PosterQuery(title="Some Film", tmdb_id="1399"))

        self.assertIsNone(wrong)
        self.assertEqual(right.urls, (f"{TMDB_IMAGE_BASE}/film.jpg",))

    async def test_a_bare_tmdb_id_never_matches_on_an_empty_name(self):
        http = FakeJsonClient(
            {_tmdb("/tv/603"): (200, {"name": "!!!", "poster_path": "/wrong.jpg"})}
        )

        match = await TmdbSource(V3_KEY, http).find(PosterQuery(title="???", tmdb_id="603"))

        self.assertIsNone(match)

    async def test_an_imdb_id_is_looked_up_without_searching_by_name(self):
        http = FakeJsonClient(
            {
                _tmdb("/find/tt3630622"): (
                    200,
                    {"tv_results": [{"name": "Detectorists", "poster_path": "/d.jpg"}]},
                )
            }
        )

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Detectorists (UK)", kind=KIND_TV, imdb_id="tt3630622")
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/d.jpg",))
        self.assertEqual(http.calls[0][1]["external_source"], "imdb_id")
        self.assertFalse(any("/search/" in url for url in http.urls))

    async def test_an_episode_id_gets_the_shows_poster(self):
        http = FakeJsonClient(
            {
                _tmdb("/find/5551234"): (
                    200,
                    {"tv_results": [], "tv_episode_results": [{"show_id": 61381}]},
                ),
                _tmdb("/tv/61381"): (200, {"name": "Detectorists", "poster_path": "/d.jpg"}),
            }
        )

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Detectorists", kind=KIND_TV, tvdb_id="episode/5551234")
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/d.jpg",))

    async def test_an_episode_id_is_not_mistaken_for_a_show_with_the_same_number(self):
        """TVDB numbers shows and episodes separately."""
        http = FakeJsonClient(
            {
                _tmdb("/find/5551234"): (
                    200,
                    {
                        "tv_results": [{"name": "Some Other Show", "poster_path": "/other.jpg"}],
                        "tv_episode_results": [{"show_id": 61381}],
                    },
                ),
                _tmdb("/tv/61381"): (200, {"name": "Detectorists", "poster_path": "/d.jpg"}),
            }
        )

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Detectorists", kind=KIND_TV, tvdb_id="episode/5551234")
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/d.jpg",))

    async def test_a_long_recording_of_unknown_kind_is_tried_as_a_film_first(self):
        """The film Fargo should not get the series' poster when TMDB has both."""
        http = FakeJsonClient(
            {
                _tmdb("/search/movie"): (200, {"results": [{"title": "Fargo", "poster_path": "/film.jpg"}]}),
                _tmdb("/search/tv"): (200, {"results": [{"name": "Fargo", "poster_path": "/series.jpg"}]}),
            }
        )
        source = TmdbSource(V3_KEY, http)

        film = await source.find(PosterQuery(title="Fargo", runtime_minutes=104))
        episode = await source.find(PosterQuery(title="Fargo", runtime_minutes=66))

        self.assertEqual(film.urls, (f"{TMDB_IMAGE_BASE}/film.jpg",))
        self.assertEqual(episode.urls, (f"{TMDB_IMAGE_BASE}/series.jpg",))

    async def test_a_year_in_the_title_picks_between_remakes(self):
        http = FakeJsonClient(
            {
                (_tmdb("/search/movie"), (("api_key", V3_KEY), ("query", "Dune"), ("year", "1984"))): (
                    200,
                    {"results": [{"title": "Dune", "poster_path": "/1984.jpg"}]},
                ),
                _tmdb("/search/movie"): (200, {"results": [{"title": "Dune", "poster_path": "/2021.jpg"}]}),
            }
        )

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Dune", kind=KIND_MOVIE, year=1984)
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/1984.jpg",))

    async def test_a_wrong_year_falls_back_to_the_plain_search(self):
        http = FakeJsonClient(
            {
                (_tmdb("/search/movie"), (("api_key", V3_KEY), ("query", "Dune"))): (
                    200,
                    {"results": [{"title": "Dune", "poster_path": "/2021.jpg"}]},
                ),
            }
        )

        match = await TmdbSource(V3_KEY, http).find(
            PosterQuery(title="Dune", kind=KIND_MOVIE, year=2026)
        )

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/2021.jpg",))

    async def test_a_title_search_only_accepts_the_same_name(self):
        http = FakeJsonClient(
            {
                _tmdb("/search/tv"): (
                    200,
                    {
                        "results": [
                            {"name": "Bluey's Big Play", "poster_path": "/wrong.jpg"},
                            {"name": "Bluey", "poster_path": None},
                            {"name": "BLUEY", "poster_path": "/right.jpg"},
                        ]
                    },
                )
            }
        )

        match = await TmdbSource(V3_KEY, http).find(PosterQuery(title="Bluey", kind=KIND_TV))

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/right.jpg",))

    async def test_a_near_miss_is_no_poster_rather_than_the_wrong_one(self):
        http = FakeJsonClient(
            {_tmdb("/search/tv"): (200, {"results": [{"name": "Bluey's Big Play", "poster_path": "/w.jpg"}]})}
        )

        self.assertIsNone(
            await TmdbSource(V3_KEY, http).find(PosterQuery(title="Bluey", kind=KIND_TV))
        )

    async def test_awkward_characters_go_out_as_a_parameter_not_glued_into_a_url(self):
        """What broke Dispatcharr's artwork: a name with special characters."""
        title = "Tom & Jerry: What's Up? #1 100%"
        http = FakeJsonClient(
            {_tmdb("/search/tv"): (200, {"results": [{"name": title, "poster_path": "/tj.jpg"}]})}
        )

        match = await TmdbSource(V3_KEY, http).find(PosterQuery(title=title, kind=KIND_TV))

        self.assertIsNotNone(match)
        url, params, _headers = http.calls[0]
        self.assertEqual(url, _tmdb("/search/tv"))
        self.assertEqual(params["query"], title)

    async def test_a_film_is_searched_as_a_film_first(self):
        http = FakeJsonClient(
            {_tmdb("/search/movie"): (200, {"results": [{"title": "Alien", "poster_path": "/a.jpg"}]})}
        )

        match = await TmdbSource(V3_KEY, http).find(PosterQuery(title="Alien", kind=KIND_MOVIE))

        self.assertEqual(match.urls, (f"{TMDB_IMAGE_BASE}/a.jpg",))
        self.assertEqual(http.urls, [_tmdb("/search/movie")])

    async def test_the_short_key_is_sent_as_a_parameter(self):
        http = FakeJsonClient()

        await TmdbSource(V3_KEY, http).find(PosterQuery(title="Bluey", kind=KIND_TV))

        _url, params, headers = http.calls[0]
        self.assertEqual(params["api_key"], V3_KEY)
        self.assertNotIn("Authorization", headers)

    async def test_the_long_token_is_sent_as_a_bearer_header(self):
        http = FakeJsonClient()

        await TmdbSource(V4_TOKEN, http).find(PosterQuery(title="Bluey", kind=KIND_TV))

        _url, params, headers = http.calls[0]
        self.assertNotIn("api_key", params)
        self.assertEqual(headers["Authorization"], f"Bearer {V4_TOKEN}")

    async def test_checking_a_key_tells_accepted_refused_and_unreachable_apart(self):
        for status, expected in ((200, True), (401, False), (503, None)):
            with self.subTest(status=status):
                http = FakeJsonClient({_tmdb("/configuration"): (status, {})})
                self.assertIs(await TmdbSource(V3_KEY, http).check_key(), expected)


class TvMazeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_tvdb_id_is_looked_up_directly(self):
        http = FakeJsonClient({f"{TVMAZE_API_BASE}/lookup/shows": (200, _tvmaze_show("Detectorists"))})

        match = await TvMazeSource(http).find(
            PosterQuery(title="Detectorists (UK)", kind=KIND_TV, tvdb_id="series/281566")
        )

        self.assertEqual(match.source, SOURCE_TVMAZE)
        self.assertEqual(http.calls[0][1], {"thetvdb": "281566"})
        self.assertEqual(len(match.urls), 2, "Full size first, the smaller one as a fallback.")
        self.assertIn("original_untouched", match.urls[0])
        self.assertEqual(match.thumbnail_url, match.urls[1])

    async def test_an_episode_id_is_not_mistaken_for_a_show_id(self):
        http = FakeJsonClient()

        await TvMazeSource(http).find(
            PosterQuery(title="Detectorists", kind=KIND_TV, tvdb_id="episode/5551234")
        )

        self.assertNotIn(f"{TVMAZE_API_BASE}/lookup/shows", http.urls)

    async def test_a_title_search_only_accepts_the_same_name(self):
        http = FakeJsonClient(
            {
                f"{TVMAZE_API_BASE}/search/shows": (
                    200,
                    [
                        {"score": 0.9, "show": _tvmaze_show("Bluey's Big Play")},
                        {"score": 0.8, "show": {"name": "Bluey", "image": None}},
                        {"score": 0.7, "show": _tvmaze_show("Bluey", "https://static.tvmaze.com/b.jpg")},
                    ],
                )
            }
        )

        match = await TvMazeSource(http).find(PosterQuery(title="Bluey"))

        self.assertEqual(match.urls[0], "https://static.tvmaze.com/b.jpg")
        self.assertEqual(http.calls[0][1], {"q": "Bluey"})

    async def test_films_are_not_its_business(self):
        http = FakeJsonClient()

        self.assertIsNone(await TvMazeSource(http).find(PosterQuery(title="Alien", kind=KIND_MOVIE)))
        self.assertEqual(http.calls, [])

    async def test_plain_http_image_links_are_upgraded(self):
        http = FakeJsonClient(
            {
                f"{TVMAZE_API_BASE}/search/shows": (
                    200,
                    [{"show": _tvmaze_show("Bluey", "http://static.tvmaze.com/b.jpg")}],
                )
            }
        )

        match = await TvMazeSource(http).find(PosterQuery(title="Bluey"))

        self.assertTrue(all(url.startswith("https://") for url in match.urls))


class SourceOrderTests(unittest.IsolatedAsyncioTestCase):
    def test_without_a_key_only_tvmaze_is_asked(self):
        self.assertEqual([s.name for s in build_sources(None, FakeJsonClient())], [SOURCE_TVMAZE])
        self.assertEqual([s.name for s in build_sources("   ", FakeJsonClient())], [SOURCE_TVMAZE])

    def test_with_a_key_tmdb_goes_first(self):
        self.assertEqual(
            [s.name for s in build_sources(V3_KEY, FakeJsonClient())], [SOURCE_TMDB, SOURCE_TVMAZE]
        )

    async def test_tmdb_wins_when_both_have_a_poster(self):
        http = FakeJsonClient(
            {
                _tmdb("/search/tv"): (200, {"results": [{"name": "Bluey", "poster_path": "/t.jpg"}]}),
                f"{TVMAZE_API_BASE}/search/shows": (200, [{"show": _tvmaze_show("Bluey")}]),
            }
        )

        match = await resolve_poster(PosterQuery(title="Bluey", kind=KIND_TV), build_sources(V3_KEY, http))

        self.assertEqual(match.source, SOURCE_TMDB)
        self.assertNotIn(f"{TVMAZE_API_BASE}/search/shows", http.urls)

    async def test_tvmaze_is_asked_when_tmdb_has_nothing(self):
        http = FakeJsonClient(
            {f"{TVMAZE_API_BASE}/search/shows": (200, [{"show": _tvmaze_show("Bluey")}])}
        )

        match = await resolve_poster(PosterQuery(title="Bluey", kind=KIND_TV), build_sources(V3_KEY, http))

        self.assertEqual(match.source, SOURCE_TVMAZE)

    async def test_a_source_that_blows_up_is_skipped_and_its_error_is_not_logged(self):
        """The error text can carry the request URL, and the URL carries the key."""

        class Exploding:
            name = SOURCE_TMDB

            async def find(self, _query):
                raise RuntimeError(f"GET {TMDB_API_BASE}/search/tv?api_key={V3_KEY} failed")

        http = FakeJsonClient(
            {f"{TVMAZE_API_BASE}/search/shows": (200, [{"show": _tvmaze_show("Bluey")}])}
        )

        with self.assertLogs("services.poster_lookup", level="WARNING") as logs:
            match = await resolve_poster(
                PosterQuery(title="Bluey"), [Exploding(), TvMazeSource(http)]
            )

        self.assertEqual(match.source, SOURCE_TVMAZE)
        self.assertNotIn(V3_KEY, "\n".join(logs.output))

    async def test_nobody_having_a_poster_is_an_ordinary_answer(self):
        self.assertIsNone(
            await resolve_poster(PosterQuery(title="News at Ten"), build_sources(V3_KEY, FakeJsonClient()))
        )


if __name__ == "__main__":
    unittest.main()
