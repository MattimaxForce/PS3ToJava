# Repository notes

The repository intentionally contains only source code and documentation.

No personal PS3 save, converted world, player names, world metadata, or local filesystem paths are included.

`core/ps3_converter.py` is the known-good conversion engine. The GUI does not reinterpret or rewrite world data; it only discovers `GAMEDATA`, selects an output location, starts the converter and presents progress.
