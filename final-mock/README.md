# final-mock

Our own mock of the bank's services for the final case (Cloudflare Workers). Not part of the builder: the builder
never reads or imports anything from here. `src/worker.js` is generic; everything case-specific is in `data.json`
(routes from the bank's integration document, answers for the bank's test customers).

    npx wrangler dev            # local test on http://localhost:8787
    npx wrangler deploy         # publish; prints the https://<name>.<subdomain>.workers.dev address
    npx wrangler tail           # watch requests while simulations run
