# String-Evidence and Confidence Policy

## Abstract

Every claim in this wiki carries one of three confidence tags - HIGH, MED, or LOW - and every backticked string is a byte-for-byte literal lifted from the binary. This page defines the three tiers, the verbatim-string rule, and the operational checks that page authors are expected to apply. It is the short, working version of the policy; the longer methodology discussion lives at [Methodology](../methodology.md).

## HIGH

A claim is HIGH when it has a byte-level anchor and at least one independent corroboration. The anchor is a verbatim string in `.rodata`, a vtable or typeinfo match against a known base class, or a structural fingerprint that has no plausible alternative interpretation. Corroboration means that two or more independent indicators (anchor plus call-site, anchor plus vtable slot, anchor plus distinctive control flow) all agree on the identification. HIGH is the default tag once a verbatim anchor exists.

## MED

A claim is MED when its evidence is structural rather than verbatim, or when a single strong piece of evidence stands without independent corroboration. Vtable shape, neighbour-function arrangement, field-offset arithmetic recovered from callers, and sibling-cloning from a HIGH-anchored template all qualify as structural. MED is the standing tag for call-graph-position identifications: a function whose role is fixed by where it sits relative to a HIGH neighbour but whose body has not been read line-by-line.

## LOW

A claim is LOW when it rests on inference from neighbouring code without a direct anchor, when evidence conflicts and the conflict is not yet resolved, or when no corroboration is available. LOW is appropriate for tiny helpers with no strings, no identified callers, no distinctive control flow, and no vtable-slot constraint. LOW evidence must not drive core prose; when LOW is the only available tag, the claim is rendered with explicit hedging or omitted entirely.

## Verbatim String Rule

Every backticked string in this wiki is byte-identical to an entry in the binary's string table, or to a substring of one. Two narrow exceptions are accepted as long as they are flagged: a printf prefix that is concatenated at runtime with a substituted suffix, and a templated instantiation where only the templated tail differs. Paraphrases and reconstructions are not backticked. Representational differences (the escape form `\n` versus the rodata byte `0x0A`) are accepted without special marking.

## Operational Checks

Page authors are expected to run these checks before merging a claim:

- When you cite a string, verify it byte-for-byte against the binary's string table. If the string is a substring or templated tail, flag it.
- When you cite a function, structure, or option by role, attach the strongest available tag (HIGH, MED, or LOW). If you cannot reach MED, omit the claim or render it with explicit hedging.
- When you cite an address-bound fact, prefer the role-level wording the rest of the wiki uses; the binary-layout and function-map pages describe the structures at the conceptual level so individual pages do not need to.
- When a contradiction surfaces between two analyses of the same construct, prefer the one with the stronger structural anchor regardless of recency, and update the errata log so dependent pages can be restamped.

Errata are tracked in the wiki's errata log alongside the verification passes; page authors propagate fixes by re-reading every page that cites a superseded identification and restamping it with the corrected tag.

The reader-side recipe for verifying a backticked string or a structural anchor against the binary directly is documented on [Binary Anatomy and RE Methodology](../topics/binary-anatomy-and-re-methodology.md).
