# Mega Man 8 - Complete

A fan project that turns **Mega Man 8 (USA, PlayStation, SLUS-00453)** into the most complete edition of the game.

## Roadmap

### Initial Release (Saturn Content Added)

1. Restore Wood Man and Cut Man fights - **100% Complete**
2. Restore "Bonus Mode" and all functionality within - **100% Complete**
3. Use Saturn enemy count and placements - **100% Complete**
4. Use Saturn text color for Robot Master intro names - **100% Complete**
5. Use Saturn's lifebar HUD - **100% Complete**
6. Add Tengu Man's Saturn Track, with ability to switch between both versions - **100% Complete**
7. Update attract mode to use the Saturn's demos (placements and recorded input) - **100% Complete**
8. Port Saturn's water effects - **100% Complete**
9. Review the sound effects and voice lines that differ between the Saturn and PSX versions and port the Saturn's where appropriate - **Not Started**
10. Assess feasibility of porting all Saturn versions of music - **Not Started**
11. Reassess the remaining color differences between the Saturn and PSX versions once all other Saturn porting is complete - **Not Started**

### Post-Release (Japanese Content Added)

12. Add the Battle & Chase and Super Adventure Rockman adverts that were present in the Japanese PSX version - **Not Started**
13. Assess feasibility to choose between Japanese intro, logo, and audio (with subtitles) and English intro, logo, and audio, effectively creating an "Undub" - **Not Started**
14. Assess feasibility to include a choice between standard subtitles and SDH for the undub - **Not Started**

### Post-Release (Quality of Life Changes)

15. Fix any grammatical issues with the dialogue - **Not Started**
16. Review HUD transparency - **Not Started**
17. Overhaul the bolt/shop system to allow for all items to be purchased in a single playthrough - **Not Started**
18. Assess the need for a full re-translation - **Not Started**

### Post-Release (Cleanup & Optimizations)

19. Assess feasibility of improving selection response time, screen transition time, and load time - **Not Started**
20. Optimize code to improve build time - **Not Started**
21. Test on real PlayStation hardware - **Not Started**
22. Assess feasibility of Mac and Linux versions of the builder - **Not Started**
23. Add a license so others can fork and modify the builder - **Not Started**

## How to use

Download the latest `mm8-complete-vX.Y.Z-windows-x64.zip` from [Releases](https://github.com/K-Wizzlejizzum/mm8-complete/releases). The builder adds the content to your own copy of the game; it contains no game data.

### What you need

- Windows 10 or 11 (64-bit). Nothing to install: Python and the compiler are included.
- About 1.5 GB of free disk space while it runs.
- Complete BIN/CUE dumps of **both** discs (every track; one .bin per track or one .bin for the whole disc, each with its .cue):

| Disc | Track | MD5 (Redump) |
|---|---|---|
| Mega Man 8 (USA), PlayStation, SLUS-00453 | 1 (data) | `4e9e6dcf27ea0e15c5f4d1f93c800378` |
| | 2 (audio) | `a75dcbb5c831a966c47563e7ea4150c9` |
| | 3 (audio) | `2d7b5e8e94a91bf5423b2356f6a34863` |
| Mega Man 8 (USA), Sega Saturn, T-1216H V1.000 | 1 (data) | `a4d575cd681b7d7c88b45d639ed7ffb7` |
| | 2 (audio) | `a2201327867523e4e31de62c445e2996` |

Track 1 of each disc must match, and a missing or extra track stops the builder. An audio track with a different MD5 only gives a note (most home rips don't correct the drive's read offset; redumper, DiscImageCreator or MPF give an exact match). Convert .chd, .pbp, .ecm or a combined image without a .cue to BIN/CUE first (for a .chd: `chdman extractcd -i "game.chd" -o "game.cue"`); an .iso can't hold this game.

### Steps

1. Unzip the whole folder somewhere you can write to (e.g. Desktop or Documents, not "Program Files").
2. Copy your two disc folders into `discs` and rename them `psx` (the PlayStation disc) and `sat` (the Saturn disc).
3. Double-click `Build Mega Man 8 - Complete.bat`. It checks both discs, then builds (usually under a minute).
4. When it says "Done", the game is in `output`: `Mega Man 8 - Complete.cue` and its three `.bin` files. Keep them together and open the .cue in your emulator (DuckStation is recommended). "(matches the tested build)" after the printed MD5 means your image is byte-identical to the tested one.

`HOW TO BUILD.txt` in the zip has the same steps and a troubleshooting list.

### Windows SmartScreen

The builder is not code-signed, so Windows may say "Windows protected your PC" the first time: click "More info", then "Run anyway". If your antivirus quarantines a file in `builder`, restore it and unzip again.

### Compiler source

The builder includes GCC and GNU Binutils (free software under the GNU GPL). Their complete source code is in the [Compiler source release](https://github.com/K-Wizzlejizzum/mm8-complete/releases/tag/gcc-12.4.0-source) as `mm8-complete-gcc-12.4.0-source.zip`. The other bundled programs (Python, NumPy, Capstone, pycdlib) keep their own licenses, in the zip's `licenses` folder.

## This repository

This repository holds only original work:

- `mm8tool.py`: the builder that ports the Saturn content and builds the PSX image from your discs (usage in its docstring)
- `helper_map.txt`: the Saturn → PSX function map the builder reads

It contains no copyrighted material: no game data, no game code and no patches. The builder reads everything it needs from your own discs (US PlayStation and US Saturn; the Japanese option will need the Japanese discs) and builds the PSX image on your machine. Don't share the images it makes.

Commit messages start with `[built]` (built and checked headlessly), `[verified]` (tested in an emulator) or `[docs]`. A `[verified]` commit marks each tested milestone.

## Disclaimer

This is an unofficial fan project. It is not affiliated with, endorsed by or sponsored by Capcom or Sega. Mega Man is a trademark of Capcom; PlayStation is a trademark of Sony Interactive Entertainment; Sega Saturn is a trademark of Sega. These names are used only to identify the games and systems the tool works with.
