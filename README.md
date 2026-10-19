# Infra Intelligence

An Astro + TypeScript research interface concept for the infrastructure economy: semiconductors, robotics, data centers, networking, and energy.

All companies, identifiers, signals, dates, chart paths, and network nodes in this prototype are clearly labeled illustrative sample data. The site does not consume live financial or infrastructure data. Newsletter sign-up is a local-only demo and does not send or store email addresses.

## Run locally

```sh
npm install
npm run dev
```

Astro prints the local preview URL (normally `http://localhost:4321`).

## Build

```sh
npm run build
npm run preview
```

The static production output is written to `dist/`.

## Cloudflare Pages

The project is configured for a static Cloudflare Pages build. Use `npm run build` as the build command and `dist` as the build output directory. `wrangler.toml` declares the Pages output directory. No deployment, paid service, or external API is configured.
