"""
Glowby agents — each stage of the fact-checking pipeline is one agent.

Pipeline (from the 4-week launch plan):

    URL in -> ingest      (download media, produce transcript)
           -> claims      (extract checkable factual claims)
           -> evidence    (gather sources per claim)         [Week 2]
           -> verdict     (rating + confidence + citations)  [Week 2]
"""
