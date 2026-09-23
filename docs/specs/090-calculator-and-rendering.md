# Calculator and Human-Friendly Rendering

## Reuse

The implementation should reuse ExpenseSyncBot's calculator behavior and tests rather than inventing arithmetic semantics again.

The existing implementation already provides safe parsing without `eval`, Big.js exact decimal arithmetic, operator precedence, percentages, currency-aware expressions, and precision regression tests.

## Separation of concerns

Calculation produces a canonical structured numeric result.

Rendering then independently produces display text and speech text for the target locale/surface.

This separation is mandatory even where the current surface uses only one representation.

## Fraction recovery

A rationalizer may represent a numeric result as a simple fraction when it is exact or within a strict configured tolerance.

The denominator is bounded to avoid absurd fractions. Continued-fraction/Fraction-style recovery is preferred over decimal string guessing.

Examples:
- exact one third → display `1/3`, speech “одна треть”;
- exact three halves → display `3/2` or `1½` by style, speech “полторы”/“три вторых” according to context;
- noisy decimal not near a simple fraction → decimal display and rounded natural speech.

If rendering rounds a non-exact value, speech explicitly conveys approximation when material.

## Spoken numbers

Speech formatting handles Russian grammatical forms, decimal fractions, percentages, currencies, large numbers, units, signs, and scientific values.

It never reads long floating-point tails digit-by-digit unless the user explicitly asks for exact digits.

## Alice mapping

Alice natively supports separate text and TTS response fields, so the fraction display and natural spoken form can be used directly.

The calculator's `RenderedNumber` is surface-neutral and also benefits Telegram voice replies and future TTS.

## Finance distinction

Generic arithmetic can return fractions.

Currency calculations preserve finance semantics and normally render decimal currency with appropriate precision; they do not turn 33.33 RUB into an arbitrary fraction of a ruble.
