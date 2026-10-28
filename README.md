# Mega Man 8 Complete

A fan project that turns **Mega Man 8 (USA, PlayStation, SLUS-00453)** into the most complete edition of the game.

Roadmap:

1. **Cut bosses** (complete): the Saturn-only Cut Man and Wood Man fights, with their stages, music, voices and bolts.
2. **Bonus Mode** (complete): the Saturn's Bonus Mode with all its screens (postcards, official illustrations, sound test, voice test and the hidden animation menu).
3. **Other Saturn / PlayStation differences**: catalogue the remaining differences between the two versions and port the ones chosen.
4. **English / Japanese option**: a way to choose between the English and the Japanese title and audio.
5. **More faithful translation** (scope to be decided).

This repository holds only original work:

- `mm8tool.py`: the toolkit that analyses the discs and builds the image (commands in its docstring)
- `helper_map.txt`, `globals_map.txt`: Saturn → PS1 function and RAM maps used by the tool

This repository contains no copyrighted material: no game data, no game code and no patches. The finished release will be a builder: you supply your own discs (US PlayStation and US Saturn; the Japanese option will need the Japanese discs), and it builds the PlayStation image on your machine.

Commit messages start with `[built]` (built and checked headlessly), `[verified]` (tested in an emulator) or `[docs]`. A `[verified]` commit marks each tested milestone.

## Disclaimer

This is an unofficial fan project. It is not affiliated with, endorsed by or sponsored by Capcom or Sega. Mega Man is a trademark of Capcom; PlayStation is a trademark of Sony Interactive Entertainment; Sega Saturn is a trademark of Sega. These names are used only to identify the games and systems the tool works with.
