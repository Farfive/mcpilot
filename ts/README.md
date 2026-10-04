# @mcpilot/sdk (TypeScript)

TypeScript SDK MCPilot: zadanie → dobór integracji MCP → autoryzacja → tylko potrzebne narzędzia → wykonanie. Ten sam kontrakt danych co SDK Python (manifesty, polityka, router, klucze poświadczeń, workflow), potwierdzony wspólnymi wektorami testowymi.

```sh
npm install
npm test
npm run example
```

- `@mcpilot/sdk` — rdzeń (bez modułów Node w ścieżce importu; stdio ładowane dynamicznie).
- `@mcpilot/sdk/node` — `EncryptedFileSecretStore` (Fernet, zgodny z Pythonem), `JsonFileWorkflowStore`, `JsonFileSnapshotStore`.

Dokumentacja: [docs/typescript.md](../docs/typescript.md).
