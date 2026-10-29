# Mega Man 8 - Complete

A fan project that turns **Mega Man 8 (USA, PlayStation, SLUS-00453)** into the most complete edition of the game.

Roadmap:

1. Restore Wood Man and Cut Man fights - 100%
2. Restore "Bonus Mode" and all functionality within - 100%
3. Use Saturn enemy count and placements - In Progress
4. Use Saturn text color for Robot Master intro names
5. Fix any grammatical issues with the dialogue
6. Add Battle & Chase and Super Adventure Rockman Adverts that were present in the Japanese PSX version
7. (Stretch Goal) Assess the need for a full re-translation
8. (Stretch Goal) Add the ability to choose between Japanese and English audio, effectively creating an "Undub".

This repository holds only original work:

- `mm8tool.py`: the toolkit that analyses the discs and builds the image (commands in its docstring)
- `helper_map.txt`, `globals_map.txt`: Saturn → PS1 function and RAM maps used by the tool

This repository contains no copyrighted material: no game data, no game code and no patches. The finished release will be a builder: you supply your own discs (US PlayStation and US Saturn; the Japanese option will need the Japanese discs), and it builds the PlayStation image on your machine.

Commit messages start with `[built]` (built and checked headlessly), `[verified]` (tested in an emulator) or `[docs]`. A `[verified]` commit marks each tested milestone.

## Disclaimer

This is an unofficial fan project. It is not affiliated with, endorsed by or sponsored by Capcom or Sega. Mega Man is a trademark of Capcom; PlayStation is a trademark of Sony Interactive Entertainment; Sega Saturn is a trademark of Sega. These names are used only to identify the games and systems the tool works with.
