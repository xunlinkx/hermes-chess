---
name: chess
description: "Use when someone wants to start, configure, resume, play, display, analyze, undo, or end a chess game. Route normal chess conversation and plausible SAN/UCI moves through the persistent local chess_game tool."
license: MIT
metadata:
  hermes:
    tags: [chess, stockfish, games, persistent]
    related_skills: []
---

# Persistent Local Chess

Use chess_game for every chess operation. Its SQLite database and
python-chess rules are authoritative; chat memory is not.

## ALWAYS check for existing game FIRST

- The pre_llm_call hook injects context about any active game at the top of
  this turn. READ that context before doing anything else.
- If the hook says "This messaging identity has an active persisted chess
  game" - do NOT create a new game. Call resume, board, or status to
  show the existing game, and treat the user's message as a move or command
  on that game.
- The /chess slash command handler is authoritative. If it processed the
  user input already, trust its result.

## Start and setup - only when NO active game exists

- Only call chess_game with action: start when the hook context does NOT
  report an existing game AND the user clearly wants a new game.
- Required choices are difficulty and human color (white, black, or
  random). Ask only for fields listed in missing_choices.
- Offer Beginner, Easy, Casual, Intermediate, Advanced, Expert, Maximum, or an
  approximate Elo. If the person says just choose, use Casual.
- Do not claim a game started until the tool returns started: true.
- If Black is chosen, the tool makes and persists Stockfish opening move.

## Play and resume

- Treat a bare plausible move such as e4, Nf3, O-O, or e2e4 as
  action: move during an active game.
- Never invent legality or a resulting position. Report a move only after the
  tool confirms it.
- For continue, board, status, or after interruption, call resume, board,
  or status. The tool safely completes a pending engine reply exactly once.
- Keep ordinary move replies concise: engine move, board, turn, check, or
  result. Give teaching detail only when requested.
- Never reconstruct FEN, PGN, or moves from remembered prose, and do not inject
  a full board or PGN into every prompt.

## Other requests

- Use get_difficulty when asked about strength. Never silently change it
  mid-game; honor confirmation_required.
- Use hint for clues and analyze for requested explanations, including Why
  did you play that?
- Use tool actions for legal moves, undo, resign, draw handling, PGN, history,
  rematches, and help.
- On rematch, ask whether to reuse settings or swap colors only when the
  request did not already specify it.

## Safety rules

- Keep identities isolated: never supply another user game ID.
- Do not pass arbitrary FEN, engine options, paths, or shell text.
- If Stockfish fails, say the game remains saved and suggest continue; do not
  replay the human move from chat.
