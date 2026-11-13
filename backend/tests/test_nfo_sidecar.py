"""Kodi .nfo sidecar rendering and writing."""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from guide_metadata import GuideMetadata
from services import nfo_writer


def _episode_details(**overrides):
    base = dict(
        show_title="Some Show",
        plot="A thing happens.",
        aired=datetime(2026, 3, 4, 20, 30),
        runtime_minutes=60,
        metadata=GuideMetadata(
            subtitle="The Episode",
            season_number=3,
            episode_number=7,
            categories=("Drama", "Mystery"),
        ),
    )
    base.update(overrides)
    return nfo_writer.RecordingDetails(**base)


class RenderNfoTests(unittest.TestCase):
    def test_episodic_recording_uses_episodedetails(self):
        root = ET.fromstring(nfo_writer.render_nfo(_episode_details()))

        self.assertEqual(root.tag, "episodedetails")
        self.assertEqual(root.findtext("title"), "The Episode")
        self.assertEqual(root.findtext("showtitle"), "Some Show")
        self.assertEqual(root.findtext("season"), "3")
        self.assertEqual(root.findtext("episode"), "7")
        self.assertEqual(root.findtext("plot"), "A thing happens.")
        self.assertEqual(root.findtext("aired"), "2026-03-04")
        self.assertEqual(root.findtext("runtime"), "60")

    def test_episode_without_subtitle_falls_back_to_the_show_title(self):
        details = _episode_details(
            metadata=GuideMetadata(season_number=1, episode_number=2)
        )
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertEqual(root.findtext("title"), "Some Show")
        self.assertEqual(root.findtext("showtitle"), "Some Show")

    def test_recording_without_season_and_episode_uses_movie(self):
        details = _episode_details(metadata=GuideMetadata(categories=("Comedy",)))
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("title"), "Some Show")
        self.assertEqual(root.findtext("premiered"), "2026-03-04")
        self.assertEqual(root.findtext("runtime"), "60")
        self.assertEqual([el.text for el in root.findall("genre")], ["Comedy"])
        self.assertIsNone(root.find("aired"))

    def test_movie_writes_every_category_as_a_genre(self):
        details = _episode_details(
            metadata=GuideMetadata(categories=("Drama", "Mystery"))
        )
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertEqual([el.text for el in root.findall("genre")], ["Drama", "Mystery"])

    def test_episode_with_only_a_season_number_is_not_episodic(self):
        details = _episode_details(metadata=GuideMetadata(season_number=2))
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertEqual(root.tag, "movie")

    def test_provider_ids_are_written_in_priority_order(self):
        details = _episode_details(
            metadata=GuideMetadata(
                season_number=1,
                episode_number=1,
                tvdb_id="12345",
                imdb_id="tt0001",
            )
        )
        root = ET.fromstring(nfo_writer.render_nfo(details))
        ids = [(el.get("type"), el.get("default"), el.text) for el in root.findall("uniqueid")]

        self.assertEqual(
            ids,
            [("tvdb", "true", "12345"), ("imdb", None, "tt0001")],
        )

    def test_tmdb_outranks_the_others_as_the_default_id(self):
        details = _episode_details(
            metadata=GuideMetadata(
                tvdb_id="12345",
                tmdb_id="678",
                imdb_id="tt0001",
                gracenote_id="EP00000001",
            )
        )
        root = ET.fromstring(nfo_writer.render_nfo(details))
        ids = [(el.get("type"), el.get("default")) for el in root.findall("uniqueid")]

        self.assertEqual(
            ids,
            [("tmdb", "true"), ("tvdb", None), ("imdb", None), ("gracenote", None)],
        )

    def test_gracenote_alone_is_written_but_never_default(self):
        details = _episode_details(metadata=GuideMetadata(gracenote_id="EP00000001"))
        root = ET.fromstring(nfo_writer.render_nfo(details))
        ids = [(el.get("type"), el.get("default"), el.text) for el in root.findall("uniqueid")]

        self.assertEqual(ids, [("gracenote", None, "EP00000001")])

    def test_recording_without_provider_ids_writes_no_uniqueid(self):
        root = ET.fromstring(nfo_writer.render_nfo(_episode_details()))

        self.assertEqual(root.findall("uniqueid"), [])

    def test_missing_fields_are_omitted_rather_than_written_empty(self):
        details = nfo_writer.RecordingDetails(show_title="Bare Show")
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("title"), "Bare Show")
        for absent in ("plot", "premiered", "runtime", "genre", "uniqueid"):
            self.assertIsNone(root.find(absent), f"{absent} should be omitted")

    def test_blank_plot_is_omitted(self):
        details = _episode_details(plot="   ")
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertIsNone(root.find("plot"))

    def test_zero_runtime_is_omitted(self):
        details = _episode_details(runtime_minutes=0)
        root = ET.fromstring(nfo_writer.render_nfo(details))

        self.assertIsNone(root.find("runtime"))

    def test_special_characters_survive_a_round_trip(self):
        details = _episode_details(
            show_title='Tom & Jerry <"Best" Of>',
            plot='He said "5 < 6 & 7 > 6".',
            metadata=GuideMetadata(subtitle="Cat & Mouse"),
        )
        rendered = nfo_writer.render_nfo(details)
        root = ET.fromstring(rendered)

        self.assertEqual(root.findtext("title"), 'Tom & Jerry <"Best" Of>')
        self.assertEqual(root.findtext("plot"), 'He said "5 < 6 & 7 > 6".')

    def test_document_starts_with_an_xml_declaration(self):
        rendered = nfo_writer.render_nfo(_episode_details())

        self.assertTrue(rendered.startswith("<?xml"), rendered[:40])


class WriteSidecarTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_sidecar_sits_beside_the_video_with_the_same_basename(self):
        video = self.tmpdir / "Some Show - S03E07.mkv"
        video.write_bytes(b"video")

        written = nfo_writer.write_sidecar(str(video), _episode_details())

        self.assertEqual(Path(written), self.tmpdir / "Some Show - S03E07.nfo")
        self.assertTrue((self.tmpdir / "Some Show - S03E07.nfo").is_file())

    def test_existing_sidecar_is_overwritten(self):
        video = self.tmpdir / "Show.mkv"
        video.write_bytes(b"video")
        sidecar = self.tmpdir / "Show.nfo"
        sidecar.write_text("stale content that is much longer than the new file")

        nfo_writer.write_sidecar(str(video), _episode_details())

        self.assertNotIn("stale content", sidecar.read_text())
        self.assertIn("<episodedetails>", sidecar.read_text())

    def test_unwritable_directory_returns_none_instead_of_raising(self):
        video = self.tmpdir / "Show.mkv"
        video.write_bytes(b"video")
        os.chmod(self.tmpdir, 0o500)
        self.addCleanup(os.chmod, self.tmpdir, 0o700)

        self.assertIsNone(nfo_writer.write_sidecar(str(video), _episode_details()))

    def test_missing_path_returns_none(self):
        self.assertIsNone(nfo_writer.write_sidecar("", _episode_details()))


if __name__ == "__main__":
    unittest.main()
