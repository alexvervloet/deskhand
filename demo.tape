# Renders demo/crash-resume.gif.
#
#   vhs demo.tape
#
# Records demo/crash_resume.py, which drives the real loop against a real
# Postgres — the numbers in the recording are read back out of the database,
# not printed from a script that already knows the answer.

Output demo/crash-resume.gif

Set Shell "bash"
Set FontSize 15
Set Width 1080
Set Height 740
Set Padding 26
Set Theme "Tomorrow Night"
Set TypingSpeed 45ms
Set Framerate 12
Set PlaybackSpeed 1.0

Hide
Type "cd /Users/alex/Documents/WebDev/AI/deskhand && clear"
Enter
Sleep 1s
Show

Type ".venv/bin/python demo/crash_resume.py"
Sleep 700ms
Enter

Sleep 30s
