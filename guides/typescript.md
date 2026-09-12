---
topic: typescript
triggers: writing or reviewing TypeScript, tsconfig choices, API response typing, async code, React state and effects, a type error that is hard to read
source: written from scratch
verified: 2026-09-12
---

# TypeScript

The type system only pays for itself if it is allowed to be strict and nobody
lies to it. Most TypeScript bugs are one of those two failing.

## tsconfig

```jsonc
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "skipLibCheck": true
  }
}
```

`strict` alone leaves the biggest hole open: without
`noUncheckedIndexedAccess`, `arr[i]` is typed `T` even when the array is empty,
so out-of-range indexed reads can pass type checking. Turn it on early; handle
`undefined` or restructure iteration so the compiler can establish safety.

`skipLibCheck` skips checking declaration files, including your own `.d.ts`
files. It can speed builds, but can hide inconsistent declarations. See the
[compiler option documentation](https://www.typescriptlang.org/tsconfig/skipLibCheck.html).

## Lying to the compiler

```ts
const user = data as User;          // asserts, checks nothing
const user = data as unknown as User;  // asserts harder, checks less
```

Both are how a runtime `undefined is not a function` gets shipped. An assertion
is a claim you are making on the compiler's behalf, and it is only safe when you
have already verified the shape.

`any` disables checking for everything it touches, including downstream
inference. `unknown` forces a narrowing step:

```ts
function handle(e: unknown) {
  if (e instanceof Error) return e.message;
  return String(e);
}
```

`catch` binds `unknown` under strict mode. Never assume a thrown value is an
`Error` — any value can be thrown, and across an async boundary it often is not.

The non-null assertion `x!` is the same lie in one character. `?.` and a real
branch cost nothing.

## Data from outside the program

Treat external data as unvalidated until its shape is checked. Assign parsed
JSON to `unknown` at the boundary even when an API returns `any`; an interface
or generic return annotation does not validate a runtime response.

Parse at the boundary with a schema validator (zod, valibot, typebox) and derive
the type from the schema so the two cannot drift:

```ts
const User = z.object({ id: z.string(), age: z.number() });
type User = z.infer<typeof User>;
const user = User.parse(await res.json());
```

Untyped `fetch<T>()` helpers are the most common source of confidently wrong
types in a codebase.

Remember `JSON.parse` does not restore `Date`, `Map`, `Set`, `BigInt`, `NaN`,
`Infinity` or `undefined`. A `Date` survives a round trip as a string.

## Modelling

Discriminated unions over optional-field soup:

```ts
type Result =
  | { status: "ok"; data: User }
  | { status: "error"; error: string };
```

This makes the impossible state unrepresentable. `{ data?: User; error?: string }`
allows both, neither, and no way for the compiler to help.

Exhaustiveness that breaks the build when a case is added:

```ts
function assertNever(x: never): never {
  throw new Error(`unhandled: ${JSON.stringify(x)}`);
}
switch (r.status) {
  case "ok": return r.data;
  case "error": return null;
  default: return assertNever(r);
}
```

Prefer a literal union or an `as const` object when no enum runtime object is
needed. `isolatedModules` rejects references to ambient `const enum` members;
locally declared const enums are allowed. Since TypeScript 5.0, numeric enums
reject out-of-domain numeric literals, although number-typed values can still
be assigned. See [isolatedModules](https://www.typescriptlang.org/tsconfig/isolatedModules.html)
and the [5.0 enum changes](https://www.typescriptlang.org/docs/handbook/release-notes/typescript-5-0.html#enum-overhaul).

`satisfies` checks compatibility without replacing the expression's inferred
type. Mutable string properties can still widen to `string`; use `as const`
when you need literal property types:

```ts
const routes = { home: "/", about: "/about" } as const satisfies Record<string, string>;
// routes.home is "/" here, not string
```

## Async

Await a promise or attach a rejection handler. `void promise` may satisfy
`@typescript-eslint/no-floating-promises`, but does not handle rejection; use
`void work().catch(reportError)` for background work with an error handler.
See the [rule's void guidance](https://typescript-eslint.io/rules/no-floating-promises/#ignorevoid).

`array.forEach(async ...)` does not wait for anything. Use `for...of` with
`await` for sequential work, or `Promise.all(array.map(async ...))` for
parallel.

`Promise.all` rejects on the first failure; it does not cancel the other work.
Use `Promise.allSettled` to await every outcome, or implement cancellation in
the operations themselves when that is required. See the
[Promise.all algorithm](https://tc39.es/ecma262/multipage/control-abstraction-objects.html#sec-promise.all).

Always give a network call a timeout — `fetch` has none by default:

```ts
const c = new AbortController();
const t = setTimeout(() => c.abort(), 10_000);
try { await fetch(url, { signal: c.signal }); } finally { clearTimeout(t); }
```

`await` in a loop is sequential. That is sometimes correct (rate limits,
ordering) and sometimes an accidental 30-second page load.

## Details that bite

- `===` always. `==` only in the idiom `x == null`, which catches both `null`
  and `undefined`.
- `Object.keys` returns `string[]`, not `keyof T`. That is deliberate, because
  objects can carry extra properties at runtime.
- Structural typing means an unrelated object with matching fields is accepted.
  Excess property checking only applies to object literals assigned directly.
- `readonly` and `as const` are compile-time only. Nothing stops a mutation at
  runtime.
- `number` covers integers, floats, `NaN` and `Infinity`. Validate ranges;
  `Number("abc")` is `NaN`, and `NaN !== NaN`.
- Array methods lie about length: `map` on a sparse array skips holes.
- `import type { X }` for type-only imports, required under
  `verbatimModuleSyntax`, and it prevents a runtime import you did not want.

## React, if present

Dependency arrays are checked by lint, not by the type system. Turn on
`react-hooks/exhaustive-deps` and fix it rather than suppressing it; a missing
dependency is a stale closure reading last render's value.

Every effect that subscribes, opens a socket, or starts a timer returns a
cleanup function. Without it, strict-mode double-invocation and every unmount
leak.

Type props explicitly. `React.FC` does not add implicit `children` with React
18+ types; declare `children?: React.ReactNode` or use `PropsWithChildren` when
the component accepts children. See the [React 18 type changes](https://react.dev/blog/2022/03/08/react-18-upgrade-guide#updates-to-typescript-definitions).

Derive state during render instead of syncing it in an effect. An effect that
only calls `setState` from other state is a rerender loop waiting to happen.

## Working with errors

Read the first line of a type error, not the last. Deeply nested "is not
assignable to" messages usually resolve to one mismatched property named at the
top. If a fix requires `any` or a double assertion, the model is wrong somewhere
upstream; fix that instead.
