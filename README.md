# Mega Man 8 - Complete

A fan project that turns **Mega Man 8 (USA, PlayStation, SLUS-00453)** into the most complete edition of the game.

Roadmap:

1. Restore Wood Man and Cut Man fights - 100% Complete
2. Restore "Bonus Mode" and all functionality within - 100% Complete
3. Use Saturn enemy count and placements - ~10%
4. Use Saturn text color for Robot Master intro names - Not Started
5. Use Saturn's lives counter placement (under life bar) - Not Started
6. Assess feasibility of porting Saturn's water effects - Not Started
7. Assess how to bring over Saturn's Tengu Man stage music without removing the PS1's version - Not Started
8. Fix any grammatical issues with the dialogue - Not Started
9. Add the Battle & Chase and Super Adventure Rockman adverts that were present in the Japanese PSX version - Not Started
10. (Stretch Goal) Assess the need for a full re-translation - Not Started
11. (Stretch Goal) Add the ability to choose between Japanese and English audio, effectively creating an "Undub" - Not Started

This repository holds only original work:

- `mm8tool.py`: the toolkit that analyses the discs and builds the image (commands in its docstring)
- `helper_map.txt`, `globals_map.txt`: Saturn → PS1 function and RAM maps used by the tool

This repository contains no copyrighted material: no game data, no game code and no patches. The finished release will be a builder: you supply your own discs (US PlayStation and US Saturn; the Japanese option will need the Japanese discs), and it builds the PlayStation image on your machine.

Commit messages start with `[built]` (built and checked headlessly), `[verified]` (tested in an emulator) or `[docs]`. A `[verified]` commit marks each tested milestone.

## Disclaimer

This is an unofficial fan project. It is not affiliated with, endorsed by or sponsored by Capcom or Sega. Mega Man is a trademark of Capcom; PlayStation is a trademark of Sony Interactive Entertainment; Sega Saturn is a trademark of Sega. These names are used only to identify the games and systems the tool works with.
