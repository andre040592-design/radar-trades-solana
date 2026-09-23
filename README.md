# Radar de trades Solana

Scanner de tokens à vista da Solana com dados públicos de GeckoTerminal e DEX Screener. GitHub Actions faz consultas agendadas a cada cinco minutos e só envia alertas por Telegram quando um sinal satisfaz os filtros. A execução agendada pode atrasar. Não executa ordens e não conecta carteiras.

## Configuração privada

Em `Settings > Secrets and variables > Actions`, cadastre `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Se desejar acompanhar preço de entrada da ZEC, adicione `ZEC_ENTRY_PRICE_USD`. **Nunca inclua esses valores em arquivos ou commits públicos.** O arquivo `config.json` contém somente filtros e a lista de endereços acompanhados.

O bot precisa receber `/start` no Telegram antes de poder enviar mensagens à conversa. Use a opção `Run workflow` na aba Actions para verificar erros de configuração; sem sinal de mercado, não há mensagem.

## Dados e limites

O scanner não verifica autoridade de emissão, concentração de carteiras, autenticidade do emissor, manipulação de volume nem slippage. Sinais são hipóteses para revisão humana. Fontes: [DEX Screener API](https://docs.dexscreener.com/api/reference), [GeckoTerminal API](https://api.geckoterminal.com/docs/index.html) e [Telegram Bot API](https://core.telegram.org/bots/api).
