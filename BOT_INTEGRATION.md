# Bot integration handoff

Please update `/home/sol/code/ircbot` so artwork admission depends on actual
rendered UTF-8 message-body length rather than a 40-column restriction.
Inspect the current files first: another agent may have changed them.
Make focused changes and preserve trusted-path artwork discovery, queuing,
cooldowns, flood pacing, CR/LF validation and the final 512-byte send_raw guard.
Do not modify img2ircart as part of this task.

The updated standalone helper is:
`/home/sol/code/img2ircart/ircart-helper.py`.
Port its changes into the bot's importable `ircbot_ircart_helper.py`, preserving
any bot-specific improvements already present. The hyphenated source filename
is not an ordinary Python import name.

Helper changes:

- parse_ircart(), load_ircart() and load_for_irc() default to a 512-column
  resource guard instead of 40. Height remains 20 lines; text remains 64 KiB.
- load_for_irc() defaults to a 400-byte body budget, replacing the previous
  unchecked None default. Explicit None still disables checking.
- Explicit max_body_bytes overrides that fallback. Each rendered line includes
  all colour controls and its reset in its reported UTF-8 size. Equality with
  the budget is accepted; overflow rejects the whole asset with row details.
- No changes to serialization, rendering, palette interpretation or sending.

In handle_ircart_command(), replace the explicit max_width=40 with the helper's
512-column guard, or omit it to use the new default. Keep height/text guards.
Treat width as a resource bound, not an estimate of message length.

Unify the byte-budget calculation used by artwork loading, queue admission,
and send_formatted_message(). Currently loading passes
get_privmsg_body_budget(target), but sending separately enforces
MAX_ART_LINE_BYTES=400 and appends an additional reset. Reserve that extra
reset byte (or remove duplication carefully if existing semantics permit).
Check the complete artwork before queuing any of its rows. Keep TXT and IRCART
consistent and reject oversized lines explicitly rather than truncating or
splitting them. Preserve useful errors without exposing unsafe paths to users.

Retain 400 as the initial configurable artwork-body policy; do not silently
raise it to the full outgoing protocol capacity. If supporting a larger policy,
calculate the allowed body as the minimum of that policy and the available
transport budget, including command, UTF-8 target, CRLF and extra formatting.
Account for the server-added sender prefix on relayed messages too; where
needed use the actual nick/user/host representation or a documented allowance.
The existing outgoing-only calculation does not prove end-to-end delivery of
a near-limit line. Do not weaken the final wire-length validation.

Real example: digitiser.png is 98 × 24 pixels. Native conversion produces
98 columns × 12 lines with a maximum body of 469 bytes under neutral settings,
either palette, without dithering. Removing the width limit alone will not make
this pass a 400-byte policy. A 469-byte allowance covers helper output only;
any sender-added bytes require further room. Report whether this particular
asset can fit the derived transport budget without promising delivery merely
because the client-to-server message fits.

Add targeted tests for:

- A 98-column flat-colour row accepted when its rendered bytes fit.
- The same width with many colour transitions rejected when it exceeds budget.
- Exact budget accepted and one-byte overflow rejected, including resets.
- Matching load/queue/send decisions, and no partial artwork queued on failure.
- TXT/IRCART parity and preserved resource guards, queue pacing and CR/LF checks.

Run the relevant bot checks and report precisely which functions changed and
whether each was partially changed or replaced. Do not commit unrelated work.

The converter now also supports --no-scaling for native pixels. Example:

```bash
python3 /home/sol/code/img2ircart/ircart.py convert \
  /home/sol/code/img2ircart/tmp/digitiser.png /tmp/digitiser.ircart \
  --no-scaling --max-width 98 --palette mirc99 --dither none --encoding rle
```
