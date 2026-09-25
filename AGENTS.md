# Shared Telegram Bot Rules

## Production release notifications

- A production deployment with user-facing changes is complete only after all eligible bot users receive one polished release notification in Russian, unless the user explicitly suppresses or postpones it.
- Treat an explicit request to deploy a bot as authorization to prepare and, after the approval gate below, send that one release notification through the repository's existing broadcast mechanism. Repository-specific rules define the audience, command, version field, and runtime evidence.
- Treat approval of the final release notification as the last user-facing gate before starting a production deployment with user-visible changes. When implementation and validation are complete and the release is ready to deploy, show the user the exact final notification that will be broadcast and wait for approval before starting the production deployment.
- The preview must be the actual final message, not a summary, rough draft, description, or approximate example. After approval, do not materially rewrite it. If a meaningful wording or content change is needed, show the revised final message and obtain approval again. The user may explicitly waive this preview/approval step for a particular release.
- Approval of the notification does not authorize announcing a failed release. After approval, deploy first, verify that the new version is running and the bot is available, and only then broadcast the approved message. Do not announce local-only work, an unsuccessful or rolled-back deployment, tests, documentation-only changes, or agent-only files.
- Treat the notification as user-facing product communication, not as a changelog dump, implementation report, or deployment status. Describe what became better, changed, or newly possible from the user's point of view.
- Choose the wording, structure, and amount of detail to fit the release; there is no mandatory template. The result must be visually clear and pleasant to read in Telegram.
- Prefer a short meaningful opening or title and sensible line breaks. When several distinct changes are being announced, bullets are the default presentation; use prose only when it is genuinely clearer.
- Do not compress several unrelated changes into one dense paragraph or send a flat status summary such as “Бот обновлён. X изменено. Y исправлено. Z добавлено.” when the same information can be presented as a readable release note.
- Group related changes together and prefer user-visible outcomes over a one-to-one transcription of completed development tasks. Keep the notification concise, but give important changes enough context for a normal user to understand why they matter or what can be done differently now.
- Restrained emoji are allowed when they improve readability and scanning. Do not decorate every line or make the message look promotional.
- Mention only deployed user-visible behavior. Omit internal terms and implementation details such as commit, deployment, API, backend, frontend, database, migration, refactor, internal service names, or agent activity unless the term is genuinely useful to the end user.
- Mention `/start` only when commands or menus changed and users may need it to refresh the interface.
- Send release notifications silently when the existing mechanism supports it. A repository-specific rule may make silent delivery mandatory.
- Make delivery idempotent for the release or version. Do not resend to everyone after an ordinary restart or repeated deployment, and retry only recipients whose earlier delivery failed.
- Verify final sent and failed counts and report only those counts to the owner as post-send evidence. Never expose bot tokens, chat IDs, usernames, or other user data.
- Preserve any explicit user instruction to suppress, postpone, customize, or waive approval for the release notification.
- Keep project-specific business rules and delivery mechanics in the repository's own `AGENTS.md`; they may specialize this contract without weakening its approval, delivery, presentation-quality, or privacy guarantees.

## Telegram Bot Button Rules

1. **Keyboard buttons = main bot navigation.**
   Use them for the main sections and the most frequently used actions.
2. **Keep the keyboard consistent.**
   Do not change it when the user moves between sections or screens.
3. **Change the keyboard only when the interaction mode changes.**
   For example, during a separate step-by-step flow. Restore the main keyboard when the flow is finished.
4. **Buttons under a message = actions for the current screen.**
   Use them for anything related to the current section, object, or message.
5. **Put the “Back” button under the message.**
   Its destination depends on the current screen.
6. **Show lists of objects under the message.**
   For example: recipes, groups, cards, banks, products, etc.
7. **Keep the keyboard short and stable.**
   Do not put every bot function there. Keep only the main entry points.
8. **Do not duplicate the same navigation in both the keyboard and under the message unless necessary.**
9. **For navigation inside one section, update the current message when possible** instead of sending a new menu message after every click.
10. **Main rule:**
    **Keyboard = where to go.**
    **Under the message = what to do here.**

> **Change the keyboard because the interaction mode changed, not because the screen changed.**

## Form flows

- In a separate form or editor flow, place cancellation only in the reply keyboard and restore the main keyboard when the flow ends.
- Keep one current form message. Edit it when possible; before sending a replacement, remove the previous bound message. If removal fails, do not create a duplicate form.
