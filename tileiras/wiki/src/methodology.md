# Methodology

This wiki documents TileIR and tileiras from the behavior outward. The private reverse-engineering corpus is used as authoring evidence, but the public pages are written as technical documentation: what the program accepts, what it produces, which invariants it enforces, and how to reimplement compatible pieces.

The most important editorial rule is that implementation archaeology is not the API. A binary address, symbol nickname, or scan artifact may explain how a fact was discovered, but it usually does not help a reader operate tileiras or build compatible tooling. Public pages therefore prefer semantic names, data contracts, algorithms, and pseudocode.

## Evidence Standard

Claims in this wiki are expected to rest on one of three kinds of evidence:

- direct behavior exposed by the driver, bytecode format, diagnostics, or emitted output,
- public or reconstructable MLIR/LLVM concepts such as dialect operations, pass contracts, verifier rules, and target attributes,
- repeated implementation evidence strong enough to describe a stable semantic contract.

When evidence is uncertain, pages should narrow the claim instead of exposing the uncertainty machinery. For example, a page should say "this pass materializes producer/consumer pipeline regions" only when that behavior is established. It should not ask the reader to reason from private scan identifiers.

## Writing Rules

Public prose should answer the reader's practical questions first:

- What is this component for?
- Where does it sit in the pipeline?
- What inputs does it accept?
- What output or IR shape does it produce?
- What invariants must hold before and after it runs?
- What algorithm would a compatible reimplementation need?
- Which neighboring pages explain the surrounding system?

Code blocks should be reimplementation-grade pseudocode. They should name data structures and steps clearly, avoid private identifiers, and keep each line within 120 characters so the rendered wiki remains readable.

## Reverse-Engineering Details

Low-level evidence still matters when it changes behavior. If a recovered detail affects compatibility, document the behavior it implies. Prefer this:

```c
bool rrt_probe(const RRT *rrt, const NodeRRT *node, int start_cycle) {
    for (int i = 0; i < node->duration; ++i) {
        int row = (start_cycle + i) % rrt->initiation_interval;

        if ((rrt->rows[row] & node->rows[i]) != 0) {
            return false;
        }
    }

    return true;
}
```

Avoid presenting the same fact as a list of private function names or address ranges. That form is useful while doing the investigation, but it is not useful documentation for readers who only see the wiki.

## Confidence and Corrections

Claims in this wiki carry confidence tags drawn from the three-tier HIGH/MED/LOW scheme defined in [String-Evidence and Confidence Policy](reference/string-evidence-and-confidence-policy.md). The wiki's working convention is:

- inline tags (HIGH, MED, LOW) are allowed and encouraged next to individual claims when the evidence basis is worth signalling to a reader,
- a deliberately authored "Confidence" or "Evidence" section is acceptable on pages whose subject is genuinely uncertain or contested, but it is not required on every page,
- core prose should still be written from the behavior outward; if a claim is only LOW, prefer to omit it or rephrase it with hedging built into the sentence rather than parking a weak claim behind a tag.

If a later investigation changes a page, update the page inline and remove the stale claim rather than preserving a historical dispute in the main prose. When a contradiction surfaces between two analyses of the same construct, prefer the one with the stronger structural anchor regardless of recency, and restamp dependent pages.

For compatibility-sensitive behavior, prefer executable-looking algorithms and explicit invariant lists. Those are easier to review and easier to port than paragraphs describing how the fact was found.

## Page Structure

Most subsystem pages should follow this shape:

1. A short purpose statement.
2. A pipeline-position diagram or paragraph.
3. The public data model or operation families.
4. Pseudocode for the central algorithm.
5. Invariants and verifier expectations.
6. Reimplementation checklist.
7. Cross-links to neighboring public pages.

Reference pages may be denser, but they should still avoid private raw evidence paths and address-heavy prose. If a table is too large to help a reader understand behavior, split it into a smaller public table and move the purely investigative detail out of the public wiki.
