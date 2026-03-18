# Code Review — Kindle Book Translator

## Resumo Geral

Projeto bem estruturado para tradução de ebooks (EPUB/PDF) via Google Translate, com UI web moderna, sistema de cache, modo bilíngue e conversão de formatos. A arquitetura é limpa e a separação de responsabilidades é boa. Abaixo estão os pontos que merecem atenção, organizados por severidade.

---

## 🔴 Problemas Críticos (Segurança / Bugs)

### 1. Memory leak no dicionário `jobs` (`app/main.py:36`)
O dicionário `jobs` cresce indefinidamente — cada tradução/conversão adiciona uma entrada que nunca é removida. Em produção, isso vai consumir memória progressivamente até crashar o processo.

**Sugestão:** Implementar TTL (time-to-live) nos jobs ou usar um `OrderedDict` com limite de tamanho. Exemplo simples com limpeza periódica:
```python
import time

MAX_JOB_AGE = 3600  # 1 hora

def cleanup_jobs():
    now = time.time()
    expired = [jid for jid, j in jobs.items() if now - j.get("created_at", 0) > MAX_JOB_AGE]
    for jid in expired:
        path = jobs[jid].get("file_path")
        if path and Path(path).exists():
            Path(path).unlink(missing_ok=True)
        del jobs[jid]
```

### 2. Arquivos temporários nunca são limpos (`app/main.py:29`)
Os arquivos em `/tmp/book_translator/` nunca são deletados. Cada job cria um arquivo que permanece no disco indefinidamente.

**Sugestão:** Deletar o arquivo após download ou implementar limpeza periódica junto com a limpeza de jobs.

### 3. CORS totalmente aberto (`app/main.py:21-26`)
```python
allow_origins=["*"]
```
Qualquer site pode fazer requisições à API. Embora aceitável para desenvolvimento, em produção isso permite que qualquer página web abuse do serviço.

**Sugestão:** Configurar origens permitidas via variável de ambiente:
```python
origins = os.getenv("CORS_ORIGINS", "*").split(",")
```

### 4. Sem rate limiting na API
Não há proteção contra abuso. Qualquer cliente pode disparar centenas de traduções simultâneas, consumindo recursos do servidor e podendo causar bloqueio pelo Google Translate.

**Sugestão:** Usar `slowapi` ou middleware customizado para limitar requisições por IP.

### 5. Cache file race condition (`app/cache.py:32-36`)
A função `_save()` usa `write_text()` que não é atômica. Se o processo morrer durante a escrita, o arquivo de cache pode ficar corrompido e todo o cache será perdido.

**Sugestão:** Escrever em arquivo temporário e depois renomear (operação atômica no mesmo filesystem):
```python
def _save() -> None:
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
        tmp.rename(CACHE_FILE)
    except Exception:
        pass
```

---

## 🟡 Problemas Médios (Robustez / Qualidade)

### 6. Docstring do cache diz MD5 mas código usa SHA256 (`app/cache.py:1-4`)
```python
"""Key = MD5(source_lang + target_lang + text)"""  # docstring
hashlib.sha256(...)  # código real
```
Docstring desatualizada e enganosa.

### 7. Função duplicada `epub_to_pdf` em dois arquivos
Existe `epub_to_pdf` tanto em `app/services/pdf_handler.py:117` quanto em `app/services/converter.py:46`. A versão no `converter.py` é melhor (tem fallback para Calibre), mas a do `pdf_handler.py` está lá sem ser usada pelo `main.py`.

**Sugestão:** Remover a versão duplicada de `pdf_handler.py`.

### 8. `_batch_translate` duplicada entre `epub_handler.py` e `pdf_handler.py`
A lógica de batch translation é praticamente idêntica nos dois handlers. Deveria ser uma função compartilhada no módulo `translator.py`.

### 9. Exceções silenciadas em vários pontos
- `app/services/cover.py:49` — `except Exception: pass`
- `app/services/pdf_handler.py:68-69` — imagens ignoradas silenciosamente
- `app/services/pdf_handler.py:100-104` — falha de inserção de texto ignorada
- `app/services/converter.py:77-78` — `except Exception: pass`
- `app/cache.py:35-36` — `except Exception: pass`

Pelo menos um `logging.warning()` ajudaria muito na depuração.

### 10. `split_text` corrompe quebras de linha (`app/translator.py:46`)
```python
sentences = text.replace("\n", " \n ").split(". ")
```
Essa lógica de split pode perder a estrutura de parágrafos do texto original. O `" ".join(translated_chunks)` na linha 107 elimina qualquer formatação original.

### 11. Bilingual CSS path relativo pode falhar (`app/services/epub_handler.py:133`)
```python
href="../bilingual.css"
```
O path relativo `../` assume uma estrutura de diretórios específica no EPUB. Dependendo da estrutura interna do EPUB, o CSS pode não ser encontrado pelo reader.

**Sugestão:** Registrar o CSS no manifest do EPUB e usar referência absoluta, ou adicionar o `<style>` inline em cada documento.

### 12. Sem validação de `target_lang` contra LANGUAGES (`app/main.py:179-205`)
O endpoint aceita qualquer string como `target_lang`. Se o valor não for suportado pelo Google Translate, o erro só aparece durante o background task.

**Sugestão:** Validar na rota antes de criar o job:
```python
if target_lang not in LANGUAGES and target_lang != "auto":
    raise HTTPException(400, f"Unsupported language: {target_lang}")
```

---

## 🟢 Problemas Menores / Melhorias

### 13. `import tempfile` não usado em `cache.py`
Linha 9 importa `tempfile` mas nunca usa.

### 14. `import re` e `import zipfile` não usados diretamente no contexto correto
Em `pdf_handler.py`, `re` e `zipfile` são importados no topo mas `zipfile` só é usado na função `epub_to_pdf` que é duplicada e possivelmente dead code.

### 15. Sem testes automatizados
O projeto não tem nenhum teste. Dado que lida com parsing de formatos complexos (EPUB, PDF) e integração com API externa, testes são essenciais para evitar regressões.

**Sugestão mínima:** Pelo menos testes unitários para `split_text`, `_key` (cache), e `_collect_blocks`.

### 16. Sem logging estruturado
O único log é um `print()` no `translator.py:79`. Usar o módulo `logging` padrão do Python facilitaria debug em produção.

### 17. `aiofiles` e `httpx` em `requirements.txt` mas não usados
Nenhum arquivo do projeto importa `aiofiles` ou `httpx`. São dependências desnecessárias.

### 18. `Pillow` em `requirements.txt` mas não usada diretamente
O PyMuPDF tem seu próprio handling de imagens. `Pillow` não é importado em nenhum arquivo.

### 19. Versão hardcoded em dois lugares (`app/main.py:19` e `app/main.py:152`)
```python
FastAPI(title="Kindle Book Translator", version="2.0.0")
# ...
"version": "2.0.0"
```
Deveria ser uma constante única.

### 20. `docker-compose.yml` usa versão deprecated
```yaml
version: "3.9"
```
O campo `version` é deprecated em versões recentes do Docker Compose.

### 21. Fly.io com 512MB pode ser insuficiente
A tradução de PDFs com PyMuPDF pode consumir bastante memória para documentos grandes. 512MB pode não ser suficiente para o limite de 50MB de upload.

---

## 💡 Sugestões de Arquitetura

### 22. Considerar usar task queue (Celery/RQ) em vez de background tasks
O `BackgroundTasks` do FastAPI roda na mesma thread/processo. Para jobs longos de tradução, isso pode impactar a responsividade do servidor. Um worker separado seria mais robusto.

### 23. Considerar tradução assíncrona com concorrência
Os blocos de texto são traduzidos sequencialmente. Usar `asyncio.gather()` com semáforo para traduzir múltiplos chunks em paralelo (respeitando rate limits) poderia acelerar significativamente livros grandes.

### 24. PDF translation perde formatação
A abordagem de recriar o PDF do zero perde fontes, layouts complexos, headers/footers, e formatação rica. Vale documentar essa limitação claramente na UI.

---

## Pontos Positivos

- **Batch translation** — Agrupar blocos com separador para reduzir chamadas de API é uma otimização inteligente e bem implementada, com fallback adequado.
- **Cache persistente** — Economiza tempo e chamadas de API em re-traduções.
- **SSE para progresso** — Mais eficiente que polling e dá boa UX.
- **Fallback Calibre → PyMuPDF** — Boa degradação graceful.
- **UI moderna e responsiva** — Single-page, dark mode, drag-and-drop, tudo bem feito.
- **Múltiplas opções de deploy** — Docker, Fly.io, Render, Heroku.
- **Modo bilíngue** — Feature diferenciada e bem implementada.
- **Código limpo e legível** — Boa organização de módulos, nomes claros, funções focadas.
