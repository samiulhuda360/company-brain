# Evaluation results

60 labelled questions (20 single-hop, 25 multi-hop, 15 aggregation). Retrieval for RAG: vector.

| | Plain vector RAG | Graph | Graph, rules-only extraction | Hybrid (graph + passages) |
|---|---|---|---|---|
| Answer accuracy | 28% | 93% | 87% | 95% |
| Single-hop accuracy | 70% | 100% | 85% | 100% |
| Multi-hop success rate | 8% | 88% | 84% | 92% |
| Aggregation accuracy | 7% | 93% | 93% | 93% |
| Citation precision | 93% | 93% | 92% | 93% |
| Citation recall | 24% | 89% | 86% | 90% |
| Answers with no citation | 29 | 3 | 4 | 2 |
| Median latency | 1.20 s | 1.53 s | 1.53 s | 1.54 s |
| 95th percentile latency | 1.50 s | 1.86 s | 1.86 s | 2.50 s |

## Entity resolution

836 labelled mentions of 167 real entities, 666 of them outside the system of record (tickets, shipments, meeting notes).

| | Pair precision | Pair recall | Wrong merges | Mentions linked to the right record |
|---|---|---|---|---|
| baseline: exact ids, emails and names | 100% | 50% | 0 | 431 of 666 |
| company-brain resolver | 100% | 96% | 0 | 659 of 666 |

## Facts from meeting notes and policies

34 labelled decision links (supplier, product, policy, customer, action owner) and 6 numeric policy terms.

| | Links found | Link precision | Link recall | Policy terms found |
|---|---|---|---|---|
| Rules only | 30 | 97% | 88% | 0 of 6 |
| Rules + model extractor | 34 | 97% | 100% | 6 of 6 |

## Graph

- Full build: 820 nodes, 2102 edges, 8535 provenance facts, 365 text passages.
- Rules-only build: 820 nodes, 2098 edges.
- Model extraction: {"calls": 5, "cache_hits": 5, "accepted": 47, "rejected": 1, "latency_s": 26.444000000000003}.
- Live model calls recorded in the cache: 141.
