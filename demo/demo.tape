# Renders the README screenshots from made-up test data (tests/fixture.py).
#   vhs demo/demo.tape      (run from the repo root)

Output demo/demo.gif

Set Shell bash
Set FontSize 15
Set Width 1240
Set Height 330
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

Hide
Type "unset CLAUDE_CONFIG_DIR XDG_CACHE_HOME; export D=$(mktemp -d /tmp/mcs.XXXX); sleep 900 & python3 tests/fixture.py $D/home $!" Enter
Type `mkdir -p $D/bin; printf '#!/bin/sh\necho "(claude $* starts here)"\n' > $D/bin/claude; chmod +x $D/bin/claude` Enter
Type "export HOME=$D/home PATH=$D/bin:$PWD:$PATH CMUX_SURFACE_ID=11111111-1111-4111-8111-111111111111" Enter
Type "cd $HOME/code/billing-api; PS1='\[\e[35m\]❯\[\e[0m\] '; clear" Enter
Sleep 500ms
Show

Type "move-claude-session"
Sleep 400ms
Enter
Sleep 1.2s
Screenshot demo/pick-session.png
Sleep 800ms

Type "inv"
Sleep 900ms
Backspace 3
Sleep 600ms
Enter
Sleep 1s
Screenshot demo/pick-account.png
Sleep 800ms
Down
Sleep 600ms
Enter
Sleep 1.5s
Screenshot demo/done.png
Sleep 2s
