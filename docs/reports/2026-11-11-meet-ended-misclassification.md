# Active meeting misclassified as ended

The owner supplied a screenshot showing room `cxf-mmbi-mvv` still active after sessions 32 and a separate diagnostic browser returned `ended`.

The diagnostic browser captured the actual rejection page:

- Heading: `You can't join this video call`.
- Buttons: `Return to home screen`, `Submit feedback`.
- Both button containers carried `data-call-ended="true"`.
- The page also contained the general access explanation, `No one can join a meeting unless invited or admitted by the host`.

The attribute describes UI on this rejection page. It does not prove that the meeting ended for its host or other participants. `_classify_denial` checked it before the existing platform-block path, so it replaced an access refusal with an unsupported meeting-wide conclusion.

The local fix uses the existing heading parser. An access-refusal heading takes precedence and follows the existing blocked/captcha checks. An ended heading still produces `ended`. The regression reuses the old `CALL_ENDED` fixture because that fixture itself contains the rejection heading and had encoded the incorrect interpretation.

Captured page evidence remains local under `.runtime/audio/diagnostics/inspect-join-cxf/page.json` and `page.png`. The underlying reason Google refused this anonymous guest remains unconfirmed; the generic access explanation does not identify a particular host setting. No new meeting link is required by this evidence.

This patch corrects reporting. It does not grant admission, alter host controls or deploy a new worker image.
