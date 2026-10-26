import { bucket, defineRailway, postgres, project, volume } from "railway/iac";
import target from "./project.json" with { type: "json" };

export default defineRailway((ctx) => {
  if (ctx.projectId !== target.projectId || ctx.environmentId !== target.environmentId)
    throw new Error("Select the dedicated Facts development project/environment.");
  // The official template owns credentials and its PostgreSQL data volume.
  const database = postgres(target.databaseName, { region: "us-east4-eqdc4a" });
  database.networking = { tcpProxies: { "5432": {} } };
  database.variables = {
    DATABASE_PUBLIC_URL: { type: "literal", value:
      "postgresql://${{PGUSER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}/${{PGDATABASE}}?sslmode=require" },
  };
  // Preserve the imported product-managed volume. This declaration is not an
  // attachment: postgres() owns that. Check live identity before any apply.
  const data = volume("postgres-volume", {
    region: "us-east4-eqdc4a", sizeMB: 50000, allowOnlineResize: true,
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
  });
  return project("Facts", { resources: [database, data, bucket(target.bucketName, { region: "iad" })] });
});
