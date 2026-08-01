# README demo, variant B: a real 708 note vault. Render with:
#   vhs assets/demo-test.tape
#
# assets/demo.tape is the fallback, it runs on examples/demo-vault instead.
#
# Runs against ~/Documents/Obsidian/test, override with SILICA_TEST_VAULT.
# The notes are Italian, the conversation is English: vault.yaml carries
# conventions.reply_language: english, while cooccurrence_lang stays italian
# because the stemmer is frozen into the index.
#
# /curate is scoped to one folder on purpose. This is a real vault with no git
# in it, so the blast radius stays at 11 notes and /revert takes them back.
#
# Sleeps are measured wall clock plus headroom (report 37s, question 31s,
# curate 18s, revert 13s, each including ~5s of startup).

Output assets/demo-test.gif

Set Shell bash
Set FontSize 16
Set Width 1000
Set Height 600
Set Padding 20
Set Framerate 12
Set PlaybackSpeed 3
Set TypingSpeed 60ms

Hide
Type `cd "${SILICA_TEST_VAULT:-$HOME/Documents/Obsidian/test}" && export SILICA_VAULT="$PWD"`
Enter
Type "clear"
Enter
Sleep 1s
Show

# 1. 708 notes that were already there. No import, no migration.
Type `find . -name '*.md' | wc -l`
Enter
Sleep 2s
Type "silica"
Enter
Sleep 15s

# 2. The answer names the note it came from, in a vault written in another language.
# The answer runs ~64 lines against a ~29 line viewport, so scroll back to its
# head and walk down. One scroll unit is one line.
Type "what do my notes say about the Atkinson and Shiffrin memory model?"
Enter
Sleep 23s
ScrollUp 40
Sleep 6s
ScrollDown 13
Sleep 6s
ScrollDown 13
Sleep 6s
ScrollDown 16
Sleep 3s

# 3. Answerable for the state of the folder, not just a reader of it.
Type "/report"
Enter
Sleep 20s
ScrollUp 40
Sleep 6s
ScrollDown 13
Sleep 6s
ScrollDown 13
Sleep 3s

# 4. It writes to real files.
Type "/curate Giurisprudenza --apply"
Enter
Sleep 17s

# 5. The gate. The writes come back out. Hold this one on screen.
Type "/revert"
Enter
Sleep 12s
