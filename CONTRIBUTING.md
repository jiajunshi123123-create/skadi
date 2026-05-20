# Contributing

Thank you for your interest in contributing to AI Data Agent!

## How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Create a Pull Request

## Development Standards

- Python code follows PEP 8
- All new features must include tests
- Prompt modifications must include before/after comparison results
- Security-sensitive changes require review from at least one maintainer

## Project Structure Notes

```
ai-data-agent/
├── agents/       # Core agent implementations (Plan, Query, Analysis)
├── config/       # Configuration files and prompts
├── knowledge/    # ChromaDB knowledge base management
├── learning/     # Self-learning and pattern storage
├── tools/        # Utility tools (RAG, query safety)
├── tests/        # Test suite
├── scripts/      # Utility scripts
└── docs/         # Documentation
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_query_agent_retry.py -v
```

## Code Review Checklist

Before submitting a PR, ensure:
- [ ] No hardcoded credentials or API keys
- [ ] All database queries are read-only (SELECT only)
- [ ] Prompt changes have been tested with at least 5 different user queries
- [ ] Error handling covers all failure modes
- [ ] New dependencies are justified and lightweight
