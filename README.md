# NEONCUT: cortes virais de gameplay

Cole um link (YouTube, Twitch, Kick, TikTok, Instagram, X, Facebook) ou envie um arquivo.
O site acha os momentos virais, legenda as falas e entrega vídeos verticais com template.

```
web/index.html     → o site (detecção, legendas com IA, templates e exportação rodam no navegador)
server/app.py      → servidor que baixa o vídeo do link e entrega um MP4 ao site
Dockerfile         → servidor + site prontos para subir
```

## Colocar no ar (Railway, cerca de 10 minutos)

1. Crie um repositório no GitHub e suba esta pasta inteira.
2. Em railway.app: **New Project → Deploy from GitHub repo** e escolha o repositório. O Railway encontra o `Dockerfile` sozinho.
3. Em **Settings → Networking → Generate Domain**, você recebe o link público do site.
4. Abra `https://SEU-LINK/api/health`. Se aparecer `"ok": true`, está tudo funcionando.

Também funciona no Render, no Fly.io ou em qualquer VPS com Docker:
`docker build -t neoncut . && docker run -p 8000:8000 neoncut`

Rodar no seu computador sem Docker (precisa de Python 3.10+ e ffmpeg instalados):

```
pip install -r server/requirements.txt
uvicorn server.app:app --port 8000
```

Depois, abra http://localhost:8000

## Configurações (variáveis de ambiente)

| Variável | Padrão | Para que serve |
|---|---|---|
| `MAX_MINUTES` | 90 | Tamanho máximo do trecho de um VOD |
| `MAX_LIVE_MINUTES` | 30 | Tempo máximo de gravação de uma live |
| `MAX_PARALLEL` | 2 | Downloads ao mesmo tempo (suba se o servidor for maior) |
| `JOBS_PER_HOUR` | 8 | Limite de downloads por pessoa (IP) por hora |
| `FILE_TTL` | 3600 | Segundos até apagar os vídeos baixados |
| `YTDLP_COOKIES` | (vazio) | Caminho de um `cookies.txt`, se o YouTube bloquear |
| `YTDLP_PROXY` | (vazio) | Proxy residencial, se o YouTube bloquear |
| `YT_ENABLED` | (vazio) | `1` liga os links do YouTube. Sem isso o YouTube fica "em manutenção" (liga sozinho se houver cookies ou proxy) |
| `DIAG_TOKEN` | (vazio) | Senha do diagnóstico `/api/diag/yt` (testa o YouTube no servidor) |
| `CORS_ORIGINS` | `*` | Domínios que podem usar a API, se o site ficar em outro domínio |

Se o site (`web/index.html`) for hospedado separado do servidor, adicione antes do `<script type="module">`:
`<script>window.NEONCUT_API = 'https://seu-servidor.com'</script>`

## Avisos importantes

- **YouTube:** o YouTube costuma bloquear servidores de nuvem e pede uma verificação anti-robô. Twitch e Kick normalmente não bloqueiam. Se acontecer, o site mostra o aviso. Para resolver, configure `YTDLP_COOKIES`, exportado de uma conta secundária, ou `YTDLP_PROXY`.
- **Lives:** a live é gravada em tempo real, então 10 minutos de live levam 10 minutos para gravar.
- **Uso:** baixar vídeos pode violar os termos de uso das plataformas. O ideal é usar com conteúdo próprio ou com autorização do criador.
- **Projeto de fãs:** não tem afiliação com a Rockstar Games nem com a Take-Two. Não use o logo nem as artes oficiais do GTA.

## Vendendo o NEONCUT (contas, planos e pagamento)

### 1. Banco de dados (obrigatório)
No Railway, abra o serviço → **Settings → Volumes → New Volume** e use o caminho **`/data`**.
Sem isso, as contas e os pagamentos são apagados a cada deploy.

### 2. Variáveis (Railway → Variables)
| Variável | Exemplo | Para que serve |
|---|---|---|
| `MP_ACCESS_TOKEN` | `APP_USR-...` | Chave de produção do Mercado Pago (Suas integrações → Credenciais de produção → Access Token) |
| `ADMIN_EMAILS` | `voce@gmail.com` | Quem vê o painel de administração em Minha conta (separe por vírgula) |
| `SUPPORT_EMAIL` | `suporte@seudominio.com` | Aparece nos termos e na privacidade |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | | Ativam o botão "Continuar com Google" |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | | Ativam as "Lives recomendadas" (app criado em dev.twitch.tv/console) |
| `BASE_URL` | `https://neoncut.up.railway.app` | Endereço público do site (use se tiver domínio próprio) |
| `PRO_INTRO_PRICE` / `PRO_PRICE` | `19.90` / `29.90` | Pro: preço promocional e preço normal |
| `PROPLUS_INTRO_PRICE` / `PROPLUS_PRICE` | `39.90` / `49.90` | Pro+: preço promocional e preço normal |
| `INTRO_MONTHS` | `3` | Quantos meses pagos têm o preço promocional |
| `HD_FREE_USES` | `5` | Exportações em 1080p por mês no Grátis e no Pro (Pro+ é ilimitado) |
| `FREE_PROJECTS` / `PRO_PROJECTS` / `PROPLUS_PROJECTS` | `3` / `100` / `250` | Vídeos por mês |
| `FREE_MAX_MINUTES` / `PRO_MAX_MINUTES` / `PROPLUS_MAX_MINUTES` | `30` / `90` / `120` | Tamanho máximo de cada vídeo |

### 3. Login com Google (opcional)
1. console.cloud.google.com → crie um projeto → **APIs e serviços → Tela de consentimento OAuth** (tipo Externo, preencha nome e e-mail).
2. **Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo da Web**.
3. Em "URIs de redirecionamento autorizados" coloque: `https://SEU-SITE/api/auth/google/callback`
4. Copie o ID e a chave secreta para `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET`.

### 4. Mercado Pago
Pro e Pro+ são vendidos como pagamento único (PIX ou cartão) que libera 30 dias. Os 3 primeiros meses pagos de cada plano saem com o preço promocional. O site confirma o pagamento sozinho
pelo retorno do checkout e pelo webhook `https://SEU-SITE/api/billing/webhook` (configurado automaticamente em cada compra).
Para dar ou tirar Pro manualmente, use o painel de administração em **Minha conta**.

## Kit de crescimento (novembro/2026)

| Variável | Padrão | Para que serve |
|---|---|---|
| `FIRST_FREE` | `1` | 1º corte baixado da conta grátis sai em 1080p e sem marca d'água (uma vez por conta). `0` desliga |
| `GA4_ID` | (vazio) | ID do Google Analytics 4 (`G-...`). Só carrega depois que a pessoa aceita os cookies |
| `GADS_ID` | (vazio) | ID do Google Ads (`AW-...`) |
| `GADS_PURCHASE_LABEL` | (vazio) | Rótulo da conversão de compra (enviada só depois que o pagamento é confirmado no servidor, com `transaction_id`) |
| `GADS_DOWNLOAD_LABEL` | (vazio) | Rótulo da conversão "primeiro download" (meta intermediária enquanto não há vendas) |
| `MP_FEE_PCT` / `INFRA_MONTH_BRL` | `4.99` / `30` | Só para o custo estimado no painel de métricas |
| `DEMO_MAX_MB` | `80` | Tamanho máximo de cada vídeo de demonstração enviado pelo admin |

- Demonstrações: Minha conta → Administração → "Vídeos de demonstração". Só vídeos próprios ou autorizados. Sem nenhum cadastrado, o site usa `web/demo/exemplo.mp4` (animação própria).
- Funil completo: `#metricas` (endpoint `/api/admin/funnel`, só administradores).
- Módulo independente: `server/growth.py`. Para desligar, remova `app.include_router(growth.router)` em `server/app.py`.
- Testes: `python3 tests/test_pagamentos.py` (pagamento simulado) e `tests/e2e.py` (Playwright, com o servidor rodando).
