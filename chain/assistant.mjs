// Starts the upstream assistant inside its container. serve.mjs binds the loopback address on
// purpose (it is a sidecar on the reader's own machine); in a container the loopback is the
// container's, so this wrapper creates the same server and listens on every interface instead.
// Compose publishes it on the host's 127.0.0.1 only, which keeps the upstream property.
import { createAssistantServer, createPairingToken, allowedOrigins } from '../assistant/serve.mjs';

const env = process.env;
const token = createPairingToken(env);
const port = Number(env.FENORIUM_ASSISTANT_PORT || 8787);
const server = createAssistantServer({ env, pairingToken: token });
server.listen(port, '0.0.0.0', () => {
  process.stderr.write(`[fenorium-assistant] listening on container port ${port}\n`);
  process.stderr.write(`[fenorium-assistant] origins=${allowedOrigins(env.FENORIUM_ASSISTANT_ORIGINS).join(',')}\n`);
});
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
