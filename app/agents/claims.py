"""
Claim extraction agent — Step 10 of the baby steps (Week 1).

Responsibility: read a transcript and extract 1-5 CHECKABLE factual
claims, skipping opinion, prediction, and satire.

Rubric (draft — refine during build):
- CHECKABLE: a statement about the world that could be verified true
  or false with evidence. ("The vaccine contains microchips.")
- NOT checkable: opinion ("this is stupid"), prediction ("X will win
  next year"), pure value judgment, obvious satire/jokes.

Status: NOT BUILT YET. Placeholder defines the contract.
"""


def extract_claims(transcript: str) -> list[dict]:
    """Return a list of {"claim", "quote", "timestamp"} dicts."""
    raise NotImplementedError("Claim extraction is built in Week 1 (baby step 10).")
