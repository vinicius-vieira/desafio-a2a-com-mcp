# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do curso de MCP e A2A. São dois processos em Python:

- `servidor-mcp/`: servidor MCP em Streamable HTTP (spec `2026-07-28`, SDK `mcp` 2.3.0) com as tools de sala, o resource da política e o ciclo de MRTR na reserva. Porta `7301`, endpoint `/mcp`.
- `agente/`: agente que é host MCP por dentro e servidor A2A v1.0 (JSON-RPC) por fora. Porta `7300`, endpoint `/a2a`, card em `/.well-known/agent-card.json`.

O agente não usa LLM: interpreta o pedido em formato fixo e traduz protocolo. Toda regra de sala (conflito, política, alternativas) é decisão do servidor MCP.

## Como rodar

Pré-requisito: [uv](https://docs.astral.sh/uv/). Ele instala o Python (3.11 ou superior) e as dependências travadas no `uv.lock` de cada pasta na primeira execução.

**Terminal 1, servidor MCP.** A chave que protege o `requestState` vem da variável `REQUEST_STATE_SECRET` e o servidor não sobe sem ela. Gere a sua e exporte:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
cd servidor-mcp
uv run servidor.py
```

Para reiniciar o servidor no meio de um MRTR (passo 12 do avaliador), pare com Ctrl+C e rode `uv run servidor.py` de novo no mesmo terminal, para manter a mesma chave. Uma chave nova invalida, de propósito, todo `requestState` emitido com a anterior.

**Terminal 2, agente.**

```bash
cd agente
uv run agente.py
```

**Terminal 3, validador**, a partir da raiz do repositório e com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Se o `python3` da máquina for anterior ao 3.10, use `uv run --no-project -p 3.12 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301`.

Variáveis de ambiente opcionais, com os padrões que o validador espera:

| Variável | Padrão | Processo |
| --- | --- | --- |
| `MCP_HOST`, `MCP_PORTA` | `127.0.0.1`, `7301` | servidor MCP |
| `AGENTE_HOST`, `AGENTE_PORTA` | `127.0.0.1`, `7300` | agente |
| `AGENTE_URL` | `http://localhost:7300` | agente (URL publicada no card) |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | agente (onde está o servidor MCP) |

### O que olhar no stderr

O servidor MCP registra uma linha por request recebido, com método, id, alvo, `traceparent` e as capabilities declaradas:

```
[mcp] request metodo=tools/list id=4 traceparent=00-f7a9f1180275b2cb314a46c9c6ae65f6-d41e8a563bdb6b14-01 capabilities={"elicitation":{"form":{}}}
[mcp] request metodo=resources/read id=5 alvo=politica://uso traceparent=00-f7a9f1180275b2cb314a46c9c6ae65f6-5ccf778d8869a279-01 capabilities={"elicitation":{"form":{}}}
[mcp] request metodo=tools/call id=6 alvo=reservar_sala traceparent=00-f7a9f1180275b2cb314a46c9c6ae65f6-670937d4db98e86b-01 capabilities={"elicitation":{"form":{}}}
[mcp] request metodo=tools/list id=7 traceparent=00-f7a9f1180275b2cb314a46c9c6ae65f6-bd4632c4b8d7a628-01 capabilities={"elicitation":{"form":{}}}
[mcp] request metodo=tools/call id=8 alvo=reservar_sala retry=sim traceparent=00-f7a9f1180275b2cb314a46c9c6ae65f6-0ec39ad4de853cb6-01 capabilities={"elicitation":{"form":{}}}
```

Esse trecho é uma Task que passou pela pausa: `tools/list` antes do `tools/call`, o mesmo trace-id do header A2A em todos os requests (com span-id novo em cada um), e o retry (`retry=sim`, id 8) com id diferente do request inicial (id 6). Requests com id numérico vêm do agente; os de id hexadecimal são do validador falando direto com o servidor.

O agente registra as transições de cada Task (`SUBMITTED -> WORKING -> INPUT_REQUIRED -> ...`).

## Onde a ponte acontece

A ponte está em `agente/agente.py`, marcada com os comentários `A PONTE, ida` e `A PONTE, volta`.

**Ida, em `_chamar_tool`.** O agente chama `tools/call` pelo cliente do SDK com `allow_input_required=True` (`agente/host_mcp.py`, `ClienteMCP.chamar_tool`), que devolve o `InputRequiredResult` cru em vez de respondê-lo por callback. Quando o resultado é um `InputRequiredResult`, `_pausa_de` extrai a chave do `inputRequests`, o campo pedido e o `enum`, guarda isso junto com o `requestState` em `tarefa.pausa` (um objeto `Pausa`, em `agente/tarefas.py`) e a Task vai para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...`. É nesse `if isinstance(resultado, types.InputRequiredResult)` que o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`.

**Volta, em `_continuar`.** O `SendMessage` com `taskId` e `escolha=<valor>` é traduzido em um `ElicitResult` (`accept` com a sala, ou `decline` para `recusar`), a Task volta para `WORKING` e `_chamar_tool` é chamado de novo com `respostas={pausa.chave: resposta}`. Ele repete o mesmo `tools/call` (mesma tool, mesmos argumentos, id JSON-RPC novo) passando `request_state=pausa.request_state`: é nessa linha que o `requestState` volta para o servidor, sem ter sido aberto nem modificado. Do outro lado, `reservar_sala` em `servidor-mcp/servidor.py` recebe o estado já verificado pelo SDK e reconstrói o pedido a partir dele.

O `requestState` nunca sai de `Pausa`: `Tarefa.publica()` monta a Task devolvida ao cliente A2A só com id, contexto, status, histórico e artifacts.

## Decisões técnicas

### Como o `requestState` é protegido

Com o utilitário do próprio SDK: `RequestStateSecurity(keys=[segredo], ttl=600)`, passado ao `MCPServer` em `servidor-mcp/servidor.py`. A tool devolve o estado em texto claro e o SDK, na fronteira do transporte, faz o resto:

- **Sela com AES-256-GCM** (AEAD), com a chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET`. Qualquer caractere trocado quebra a tag de autenticação e o retry recebe `-32602`, sem chegar à tool. De quebra o conteúdo fica ilegível para o cliente.
- **Carimba a expiração.** Vale **10 minutos** (`VALIDADE_DO_REQUEST_STATE`). Expirado, `-32602`.
- **Amarra ao request.** O envelope leva o método, o nome da tool e um digest dos argumentos. Um retry com argumentos diferentes dos originais é rejeitado com `-32602`.

A chave vem só do ambiente, com no mínimo 32 bytes; sem ela o processo termina com uma mensagem dizendo como gerar. Não há segredo no código nem no repositório.

O conteúdo selado é o pedido inteiro mais as alternativas oferecidas (`PerguntaSelada`: `sala`, `inicio`, `fim`, `responsavel`, `alternativas`). No retry, `reservar_sala` usa esses valores, e não os argumentos reenviados, e valida a escolha contra as alternativas seladas. O servidor não guarda nada entre o `input_required` e o retry, então um retry depois de um restart funciona. Se a alternativa escolhida tiver sido ocupada nesse meio-tempo, o servidor responde um novo `input_required` com as alternativas recalculadas.

**Por que não os resolvers do SDK (`Resolve`/`Elicit`).** Foi a primeira versão, e passava no validador. Mas o resolver sela só um digest da pergunta e recalcula as alternativas no retry: depois de um restart as reservas em memória somem, as alternativas mudam, a pergunta deixa de bater e o SDK pergunta de novo em vez de concluir. Isso quebra o passo 12 do avaliador sempre que alguma alternativa tiver sido reservada antes. O fluxo manual, em que a tool devolve o `InputRequiredResult`, deixa o servidor escolher o que vai selado. A checagem de capability, que o resolver fazia sozinho, ficou em `_exigir_elicitation_em_formulario`, que responde `-32021` com `data.requiredCapabilities`.

### Onde fica o estado das Tasks

Em memória, no processo do agente: um dicionário `task id -> Tarefa` em `agente/tarefas.py`. Cada `Tarefa` tem a parte pública (id, contextId, estado, mensagem de status, histórico, artifacts) e a parte que é só do agente (tool e argumentos do `tools/call` original, trace-id, versão da política lida do resource e a `Pausa`). O estado pausado é por Task, então duas Tasks pausadas ao mesmo tempo não trocam de `requestState`. As transições passam por `Tarefa.mover`, que só aceita as arestas da tabela `TRANSICOES`; estado terminal não tem aresta de saída, e um `SendMessage` para uma Task terminal é recusado com `-32004` (`UnsupportedOperationError`).

Reiniciar o agente perde as Tasks. Reiniciar o servidor MCP não: a Task pausada continua retomável, porque o estado do MRTR viaja no `requestState`.

### Outras decisões

- **A2A sem SDK.** O binding JSON-RPC pedido (Agent Card, `SendMessage`, `GetTask`) cabe em um arquivo com Starlette, e assim a forma do fio fica idêntica à de `exemplos/wire/`. `GetTask` devolve `{"task": {...}}`, como no exemplo 09. O `SendMessage` é bloqueante: responde quando a Task chega a um estado terminal ou interrompido.
- **Descoberta em runtime.** A cada Task o agente faz `tools/list`, confere que a tool da skill foi anunciada e monta os argumentos a partir do `inputSchema` descoberto. Depois lê `politica://uso` e extrai a versão da primeira linha: é esse valor que vai no campo `politica` do artifact.
- **Trace.** O trace-id do header `traceparent` do primeiro `SendMessage` fica na Task e vai no `_meta` de todos os requests MCP dela, inclusive os da retomada, com span-id novo em cada um. Sem header, o agente sorteia um trace-id para a Task.
- **Erros.** `isError: true` da tool termina a Task em `FAILED` com o texto da tool no status e no histórico. Erro de protocolo ou de transporte ao falar com o MCP também termina em `FAILED`, com o motivo. Recusa (`reservado: false`) termina em `CANCELED`.
- **Log do servidor.** Um middleware ASGI por fora do SDK, para registrar também os requests que o transporte recusa antes de chegar a um handler (por exemplo, `_meta` incompleto).

### Limitações do SDK encontradas

1. **O cliente não declara elicitation só em form mode.** `ClientSession._build_capabilities` só anuncia elicitation quando há um `elicitation_callback`, e aí anuncia os dois modos:

   ```python
   elicitation = (
       types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
       if self._elicitation_callback is not _default_elicitation_callback
       else None
   )
   ```

   O agente não quer callback (ele responderia a pergunta sozinho, sem pausar a Task) nem atende url mode. `SessaoComElicitationEmFormulario`, em `agente/host_mcp.py`, sobrescreve esse método para declarar exatamente `{"elicitation": {"form": {}}}`.

2. **O transporte Streamable HTTP do cliente não sobrevive a um erro de conexão.** Uma falha de POST derruba o task group do transporte e a sessão passa a responder `Connection closed` para sempre. Com uma sessão única, o agente ficava mudo depois de um restart do servidor MCP. Por isso o agente abre uma sessão do SDK por request A2A e mantém vivo só o `httpx2.AsyncClient`. Como a revisão `2026-07-28` não tem handshake, isso não custa nenhum request a mais.

3. **O contador de ids JSON-RPC é por sessão.** Com sessões curtas, o retry poderia repetir o id do request inicial. `DespachanteComIdsDoProcesso` sobrescreve `JSONRPCDispatcher._allocate_id` com um contador do processo.

4. **Sem `tools/list` na sessão, o SDK lista por conta própria.** `ClientSession.validate_tool_result` chama `self.list_tools()` para obter o `outputSchema`, em um request sem o `traceparent` da Task. O agente faz o `tools/list` explicitamente em toda sessão, com o `traceparent`, antes do `tools/call`.

5. **O SDK prefixa os erros de execução** com `Error executing tool <nome>: `. As mensagens do enunciado vêm inteiras depois do prefixo, e o agente repassa o texto da tool sem editar.

Os itens 1 e 3 sobrescrevem métodos com `_` no nome, então precisam ser revistos ao atualizar o SDK (a versão está travada em `2.3.0`).

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de um clone limpo:

```
trace-id desta execucao: 8529c3443b7e40a940a3a6512a16aa49
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
