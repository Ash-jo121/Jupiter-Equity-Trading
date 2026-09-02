import { env } from 'cloudflare:workers';

const bindings = env as unknown as Record<string, string | undefined>;

async function proxy(request: Request): Promise<Response> {
  const backend = bindings.JUPITER_BACKEND_URL;
  if (!backend) {
    return Response.json(
      { detail: 'Dashboard backend is not configured' },
      { status: 503 },
    );
  }

  const incoming = new URL(request.url);
  const upstream = new URL(
    incoming.pathname.replace(/^\/api(?=\/|$)/, '') + incoming.search,
    backend.endsWith('/') ? backend : `${backend}/`,
  );
  const headers = new Headers(request.headers);
  headers.delete('cookie');
  headers.delete('host');
  headers.delete('oai-authenticated-user-email');
  headers.delete('oai-authenticated-user-id');

  const response = await fetch(upstream, {
    method: request.method,
    headers,
    body:
      request.method === 'GET' || request.method === 'HEAD'
        ? undefined
        : await request.arrayBuffer(),
    redirect: 'manual',
  });
  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete('set-cookie');
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
