import dataclasses
from jellyfin_apiclient_python import JellyfinClient  # type: ignore


@dataclasses.dataclass
class JellyfinLibraryClient:
    client: JellyfinClient
    library_id: str
    address: str


def jf_best_thumbnail_url(client: JellyfinClient, item: dict) -> str | None:
    priority = ["Primary", "Backdrop", "Logo"]

    priority_map: dict[str, int] = {}
    for index, e in enumerate(reversed(priority)):
        priority_map[e] = index

    keys = item["ImageBlurHashes"].keys()
    if len(keys) == 0:
        return None

    artwork_name = max(keys, key=lambda x: priority_map.get(x, -1))
    return client.jellyfin.artwork(
        item["AlbumId"],
        art=artwork_name,
        max_width=720,
    )
