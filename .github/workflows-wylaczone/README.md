# Workflowy wyłączone

GitHub uruchamia tylko pliki z `.github/workflows/`, więc te nie działają. Były ręcznymi
workflowami diagnostycznymi (`workflow_dispatch`), żaden nie jest wołany przez pg_cron ani
przez inny workflow.

Żeby któryś włączyć, przenieś go z powrotem do `.github/workflows/` i przed tym:

- zamień `npm install ...` na `npm ci` w katalogu z lockiem (`scraper/olx-reveal`, `scraper/olx-mobile`, `scraper/fp-probe`);
- dodaj `permissions: contents: read`;
- przypnij akcje przez pełne SHA, jak w workflowach w `.github/workflows/`.

`browser-probe-403` instaluje patchright w `scraper/olx-mobile`, którego lock go nie zawiera.
Trzeba go dopisać do `package.json` i locka tego katalogu.
