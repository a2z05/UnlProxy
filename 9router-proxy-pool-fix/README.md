# 9router proxy-pool fix: a health test must not disable the pool

A one-line bug in 9router, and a patch for it.

## The bug

`POST /api/proxy-pools/[id]/test` writes the result of the test over the pool's
`isActive` flag:

```js
await updateProxyPool(id, {
  testStatus: result.ok ? "active" : "error",
  lastTestedAt: now,
  lastError:  result.ok ? null : (result.error || `...`),
  isActive:   result.ok,          // <-- this line
});
```

So **one failed test switches the pool off**. Nothing in 9router re-tests
automatically — there is no scheduled job, no retry, no health poll. A pool that
fails once stays inactive until a human toggles it back on in the dashboard.

That matters because a single failure is usually transient. A rotating egress
node is unreachable for a few seconds every rotation; a slow moment times out an
8-second test; the test URL itself is briefly blocked. Any of those silently
removes a perfectly good pool from rotation, and the only clue is a small
`inactive` badge.

The UI already assumes the test does *not* do this. After a bulk health check it
shows a prompt — *"Alive: 3, Dead: 1. Disable 1 dead proxies?"* — i.e. turning a
pool off is a decision the user makes, not something the test does for them.

## The fix

Stop writing `isActive` from the test. A test reports health; `testStatus`
carries the verdict. `isActive` stays the user's on/off switch.

## Apply it

### npm / `npx` install (most common)

npm ships the compiled server only, so patch the build.

**Windows** — double-click `fix-9router.bat`, or from a terminal:

```bat
fix-9router.bat            :: find your 9router, patch it, verify it
fix-9router.bat --check    :: is it patched?
fix-9router.bat --revert   :: undo
```

**Linux / macOS / any shell with node** — the same thing:

```bash
./fix-9router.sh           # patch
./fix-9router.sh --check   # is it patched?
./fix-9router.sh --revert  # undo
```

Both are thin wrappers around `apply.mjs`, which is the actual implementation —
the install-finding, matching and parse-checking are JavaScript, and a batch or
shell re-write would be a second copy of those rules to drift out of sync.
`node apply.mjs` directly does exactly the same thing.

If your 9router is somewhere unusual:

```bash
node apply.mjs --dir /path/to/9router
```

Then **restart 9router** so the new build is loaded.

### Source checkout

Use the patch if you build 9router yourself:

```bash
git apply 9router-proxy-pool-test.patch
```

Or run `node apply.mjs --dir .` — it patches `src/...` when that exists.

## What the script guarantees

- **Finds 9router itself** — `NINEROUTER_DIR`, the npm global prefix on Windows
  and Unix, `node_modules/9router` in the current directory, then `npm root -g`.
- **Recognises both shapes** — `src/…/route.js` (source, multi-line) and
  `app/.next-cli-build/server/…/route.js` (minified build). Different forms,
  same statement.
- **Proves the result parses** before writing it. A broken build route takes the
  whole 9router API down, so "the string changed" is not good enough.
- **Skips rather than guesses** on an unrecognised version: if neither the known
  statement nor the marker is present, it changes nothing and says so.
- **Leaves a `route.js.bak`** and refuses to revert from one that is not the
  original file.

Tested against 9router **0.5.112** (the version this was written against),
verified live: with the patch, a failing test returns `ok:false` while
`isActive` stays `1` and `testStatus` becomes `error`. Without it, the same test
sets `isActive` to `0`.

## After patching

A failed test now shows `testStatus = error` on a pool that is still enabled. If
you *want* a dead pool out of rotation, turn it off — the toggle in the pools
list, or the "Disable Dead Proxies" prompt after a bulk health check. Both still
work exactly as before; they are the only things that change `isActive` now.

## Upstream

This is a patch, not a fork. It is written to apply to `decolua/9router` and to
be discarded once the fix lands upstream. Nothing here is 9router's code beyond
the one statement being changed.

## Files

| file | what |
|---|---|
| `apply.mjs` | the implementation: install / check / revert, npm installs and source checkouts |
| `fix-9router.bat` | Windows launcher — double-clickable |
| `fix-9router.sh` | Linux / macOS launcher |
| `9router-proxy-pool-test.patch` | `git apply`-able diff against the 9router source |
| `README.md` | this |
