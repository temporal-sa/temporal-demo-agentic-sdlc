# Sample PR: bulk discount

The review scenario opens a pull request with these files on top of `main`, as a person would.
It looks reasonable and its tests pass, but it has bugs a careful reviewer should catch:

- "3 or more items" is checked with `>`, so a cart of exactly 3 gets no discount, and the
  tests only try 1 and 4 items.
- Tax is charged on the undiscounted subtotal.
- The discount isn't rounded to cents (`roundCents`), so some carts show fractions of a cent.

`scripts/setup-github.sh` pushes them to the storefront's `samples/discount-rounding` branch,
which `open_sample_pr` copies for each run.
