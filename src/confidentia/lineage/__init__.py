"""C3: cryptographically verifiable lineage.

A lineage table records what happened. This records what happened *and makes
altering the record detectable* -- the difference between provenance you can read
and provenance you can rely on.

    merkle.py    Trees over record digests, with O(log n) inclusion proofs.
    chain.py     What a provenance entry says, how it is linked and signed. Pure.
    recorder.py  Digest a table, bind it to code and keys, append under a lock.
    verify.py    Recompute the chain, the columns and -- on demand -- the data.

Under the `sim` backend the platform measures itself and holds its own signing key.
See `chain.py` for what that does and does not establish; every entry carries
`hardware_rooted = false` and no claim is made beyond it.
"""
