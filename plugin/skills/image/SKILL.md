---
name: image
description: Generate an image with Gemini through agent-intern's antigravity_image tool and save it into the project. Use when the user wants an image, icon, illustration, mockup, banner or hero graphic. Claude Code has no image model of its own.
argument-hint: "<what to draw> [where to save it]"
---

Generate an image for: $ARGUMENTS

1. **Pick the output path.** If the request names one, use it. Otherwise choose a descriptive kebab-case
   file name. Put it in an existing `assets/`, `images/`, `public/` or `docs/` directory if the
   project has one, else in the project root. Pass it as an absolute path.
2. **Write a concrete prompt.** Cover the subject, style, composition, colour palette, background and
   aspect ratio. Keep any text the image must show short and quote it exactly, because image models
   misspell long text.
3. **Call `antigravity_image`** with that prompt, the `output_path`, and `workspace` set to the project
   root. For several variations, call `antigravity_image_swarm` instead, with one prompt per
   variation.
4. **Use the path the tool returns.** Gemini picks PNG or JPEG itself, and the tool renames the file to
   match the real bytes, so `hero.png` may come back as `hero.jpg`.
5. **Look at the result.** Read the saved image and check it matches the request. If it clearly misses,
   for example the wrong subject or garbled text, say what's wrong and offer one retry with an
   adjusted prompt. Don't retry silently in a loop.

This needs the Antigravity backend: `agy`, signed in to Google AI Pro. If `antigravity_image` reports
that agy is missing or not signed in, tell the user and suggest `/agent-intern:doctor`.
