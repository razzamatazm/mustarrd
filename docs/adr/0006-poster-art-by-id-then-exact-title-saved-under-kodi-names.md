# Poster art is matched by ID, then by exact title, and saved under Kodi's file names

## Context

Poster art (#443) fetches a poster for each finished recording, shows it in
History and saves a copy into the library. Three things about it were not
obvious and are easy to "fix" later in a way that brings back the problem they
avoid.

**Which poster.** `GuideMetadata` stores TMDB, TVDB and IMDb IDs when a guide
publishes them, and an ID cannot land on the wrong show. In practice most
Xtream guides publish none, so an ID-only feature would produce posters for
almost nobody. The alternative is a search by name, and name search is where
Dispatcharr's v0.21.0 artwork bug came from: titles with special characters
broke the lookup, and a loose match puts another show's poster on a recording.

**Whom to ask.** The owner's call: TMDB when the user has saved a key, then
TVMaze, which needs no key but only knows TV shows. OMDb (its own key) and
iTunes (name search only) were considered and left out. Asking either service
tells a third party what was recorded.

**Where the file goes.** Kodi, Jellyfin and Plex's NFO agents agree on two
names: `poster.jpg` in a show's folder, and `<video name>-poster.jpg` beside a
single video. Plex's own "local media assets" reads `<video name>.jpg` beside
an episode as that episode's *thumbnail*, so a show poster under that name
would be shown as a stretched still. And the completed folder's layout is the
user's: the default TV template is flat, with every show in one folder.

## Decision

- **ID first, exact title second.** Each source is asked by ID when there is
  one. Otherwise it is searched by the cleaned title (numbering, "New:" tags
  and trailing brackets removed), and a result is accepted only when its name
  equals the recording's once case, accents and punctuation are set aside. A
  bare TMDB number of unknown kind must also agree on the name, because TV
  1399 and film 1399 are different things. The title always travels as a
  request parameter, never glued into a URL.
- **No match means no poster.** It is not an error, it is not retried on a
  timer, and it is never "closest result wins".
- **It runs off the recording event seam**, after the recording is finished,
  as the last subscriber and under a time limit. It cannot delay or fail a
  recording, and it cannot hold up webhooks or notifications behind it.
- **It can be turned off**, and saving into the library is a second switch.
  Both default to on. The settings page says what is sent and to whom.
- **Third-party services are told who is asking.** Lookups and image fetches
  send `Mustarrd (poster lookup)`, not the player-style User-Agent providers
  get (#453). The image fetch reuses `logo_cache`'s guard: public addresses
  only, every redirect re-checked, a size cap.
- **Kodi's names.** An episode whose folder (or whose `Season NN` folder's
  parent) is named after the show gets `<show folder>/poster.jpg`. Everything
  else gets `<recording name>-poster.jpg`. An existing show poster is left
  alone unless the user presses Refresh artwork.
- **The TMDB key is a secret.** Encrypted at rest like account passwords,
  write-only through the API, and kept out of logs: a failed lookup logs the
  exception's type, not its text, because the text can carry the request URL.

## Consequences

- Recordings whose guide title differs from the canonical one ("Detectorists
  (UK)" is handled; "Det. S2" is not) get no poster. That is the intended
  trade. Loosening the match to fix it brings back wrong posters; the route to
  better coverage is more IDs in the guide, or a manual pick.
- Films get nothing without a TMDB key.
- In a flat or channel-grouped library, each episode gets its own
  `-poster.jpg`, which Kodi and Jellyfin ignore for episodes. Users who want
  show posters in their library need a `{show}/...` folder in the TV template.
  Guessing a shared folder is the show's would put one show's poster on
  everything else in it.
- Upgrading turns on outbound requests to TVMaze for every finished recording
  until the user switches it off.
- On-demand (VOD) downloads are skipped: their rows carry no guide metadata
  and, for episodes, not the show's name.
