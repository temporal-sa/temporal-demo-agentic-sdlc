# Orbit Supply Co. storefront

A static storefront with no build step and no dependencies.

- `index.html`, `styles.css`: the page.
- `src/cart.js`: pure cart logic (no DOM). Put business rules here and test them.
- `src/app.js`: DOM rendering only.
- `src/telemetry.js`: reports page errors to the hosting environment. Leave it in place.
- `tests/*.test.js`: `node:test` suites. Run them with `npm test` (`node --test tests/`).

Rules:
- Don't add npm dependencies; use only the browser and Node standard library.
- Every change to `src/cart.js` comes with tests.
- Keep the page working without a server-side component: it's served as static files.
