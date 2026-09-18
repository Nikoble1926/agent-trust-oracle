# Dockerfile for the Glama MCP listing. Builds the TypeScript stdio MCP server
# in mcp/ and runs it on stdio. The free get_trust_preview tool works with no
# wallet key; the paid tool activates only when EVM_PRIVATE_KEY is set at
# runtime, which is deliberately never baked into the image.
FROM node:18-alpine AS build
WORKDIR /app
COPY mcp/package.json mcp/package-lock.json mcp/tsconfig.json ./
RUN npm ci
COPY mcp/server.ts ./
RUN npm run build

FROM node:18-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
# stdio MCP server; starts and answers MCP introspection with no key.
CMD ["node", "dist/server.js"]
