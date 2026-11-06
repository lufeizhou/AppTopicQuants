# How to report to the user

The user is not a data scientist. Every answer about this project follows these rules:

1. **Main point first**, in one or two plain sentences: what happened or what it means.
2. **Why** (the rationale) in a sentence or two.
3. **A real example** from the data, e.g. a real search and the real apps on its page. Examples beat definitions.
4. **Only the main details**: the few numbers that change a decision (before → after, cost, accuracy). No thresholds,
   token counts, file paths, function names, row counts or side findings unless asked. Those belong in
   docs/CHANGELOG.md, not in the chat.
5. **Decisions the user must make**: few, each in plain words with the options, what each option changes (with an
   example), and my recommendation. Never ask about something the user can't judge without reading code; decide
   it myself and say so in one line.
6. Short. If it needs more than one screen, it is too long: cut the detail, keep the example.
7. Plain words for steps: "who really competes for a search", not "Step 2 relevance judge".
