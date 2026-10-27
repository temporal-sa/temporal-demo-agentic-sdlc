# Orbit Supply Co. storefront

A static storefront with no build step and no dependencies.

- `index.html`, `styles.css`: the page.
- `src/cart.js`: pure cart logic (no DOM). Put business rules here and test them.
- `src/app.js`: DOM rendering only.
- `src/telemetry.js`: reports page errors to the hosting environment. Leave it in place.
- `tests/*.test.js`: `node:test` suites. Run them with `npm test` (`node --test tests/`).

- Dependencies: `orbit-money` (money formatting), declared in `package.json` for Node and in the
  import map in `index.html` for the browser. The two versions must always match. Run `npm install`
  before the tests; a dependency's changelog is at `node_modules/<name>/CHANGELOG.md`.

Rules:
- Don't add new dependencies; use the browser and Node standard library.
- Every change to `src/cart.js` comes with tests.
- Keep the page working without a server-side component: it's served as static files.
