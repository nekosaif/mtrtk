/**
 * Contract test: every route the TypeScript client references exists in the daemon's OpenAPI
 * schema, with the method and request-body shape the client sends. `openapi.snapshot.json` is
 * a copy of `GET /api/openapi.json`; regenerate it with
 * `uv run python web/scripts/gen_openapi_snapshot.py` from the repository root.
 */
import snapshot from "./openapi.snapshot.json";
import { ROUTES } from "./api";
import type {
  ConfigBody,
  FreezeBody,
  KeepBody,
  LoginBody,
  ModeBody,
  PollBody,
  ResetBody,
  SiteBody,
} from "./types";

interface Operation {
  parameters?: { name: string; in: string; required?: boolean }[];
  requestBody?: { content: Record<string, { schema: { $ref?: string } }> };
}
interface Schema {
  properties?: Record<string, unknown>;
  required?: string[];
}
const paths = snapshot.paths as unknown as Record<string, Record<string, Operation>>;
const schemas = (snapshot.components as { schemas: Record<string, Schema> }).schemas;

// The keys each TypeScript body type carries: `keyof` binds the list to the type, and the test
// binds the list to the schema. A field added on either side alone fails here.
const BODY_KEYS: Record<string, string[]> = {
  LoginBody: ["password"] satisfies (keyof LoginBody)[],
  ConfigBody: ["values"] satisfies (keyof ConfigBody)[],
  ResetBody: ["kind"] satisfies (keyof ResetBody)[],
  PollBody: ["msg_class", "msg_id"] satisfies (keyof PollBody)[],
  ModeBody: ["mode", "svin_min_duration_s", "svin_acc_limit_m", "site"] satisfies (keyof ModeBody)[],
  FreezeBody: ["name", "activate"] satisfies (keyof FreezeBody)[],
  SiteBody: ["name", "x", "y", "z", "lat", "lon", "height_m", "sigma_m", "source", "frame", "epoch", "notes"] satisfies (keyof SiteBody)[],
  KeepBody: ["keep"] satisfies (keyof KeepBody)[],
};

// Query parameters the client builds, per route name.
const QUERY_PARAMS: Partial<Record<keyof typeof ROUTES, string[]>> = {
  history: ["metrics", "from", "to", "res"],
  events: ["limit", "level"],
  jobs: ["kind", "limit"],
  ntripHistory: ["limit"],
  logsAvailability: ["from", "to"],
  logsWindow: ["from", "to"],
  deleteLog: ["force"],
};

describe("API contract (openapi.snapshot.json)", () => {
  it("is the daemon's 34-path inventory", () => {
    expect(Object.keys(paths)).toHaveLength(34);
    expect(paths["/healthz"]?.get).toBeDefined();
  });

  it.each(Object.entries(ROUTES))("%s exists with its method", (_name, spec) => {
    const ops = paths[spec.path];
    expect(ops, `no path ${spec.path}`).toBeDefined();
    expect(ops[spec.method.toLowerCase()], `${spec.method} ${spec.path}`).toBeDefined();
  });

  it("references every path the schema has (a new backend route must reach the client)", () => {
    const referenced = new Set<string>(Object.values(ROUTES).map((r) => r.path));
    expect([...Object.keys(paths)].filter((p) => !referenced.has(p))).toEqual([]);
  });

  it.each(Object.entries(ROUTES).filter(([, spec]) => "body" in spec))("%s sends the body schema the route declares", (_name, spec) => {
    const op = paths[spec.path][spec.method.toLowerCase()];
    const ref = op.requestBody?.content["application/json"]?.schema.$ref ?? "";
    const body = (spec as { body: string }).body;
    expect(ref.endsWith(`/${body}`), `${spec.method} ${spec.path} takes ${ref}, client sends ${body}`).toBe(true);
    const schema = schemas[body];
    const clientKeys = BODY_KEYS[body];
    expect(clientKeys, `no BODY_KEYS entry for ${body}`).toBeDefined();
    const schemaKeys = Object.keys(schema.properties ?? {});
    // nothing the client sends is unknown to the server, and nothing required is missing
    expect(clientKeys.filter((k) => !schemaKeys.includes(k))).toEqual([]);
    expect((schema.required ?? []).filter((k) => !clientKeys.includes(k))).toEqual([]);
    // and the client's type is not missing an optional field the server accepts either
    expect(schemaKeys.filter((k) => !clientKeys.includes(k))).toEqual([]);
  });

  it("every body schema in the snapshot is one the client types", () => {
    const bodies = Object.keys(schemas).filter((n) => n.endsWith("Body"));
    expect(bodies.sort()).toEqual(Object.keys(BODY_KEYS).sort());
  });

  it.each(Object.entries(QUERY_PARAMS))("%s query parameters are declared", (name, params) => {
    const spec = ROUTES[name as keyof typeof ROUTES];
    const op = paths[spec.path][spec.method.toLowerCase()];
    const declared = (op.parameters ?? []).filter((p) => p.in === "query").map((p) => p.name);
    for (const p of params ?? []) expect(declared, `${name}: ?${p}`).toContain(p);
  });

  it("path parameters match the template names", () => {
    for (const spec of Object.values(ROUTES)) {
      const op = paths[spec.path][spec.method.toLowerCase()];
      const declared = (op.parameters ?? []).filter((p) => p.in === "path").map((p) => p.name).sort();
      const inTemplate = [...spec.path.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
      expect(declared, spec.path).toEqual(inTemplate);
    }
  });

  it("the reset body enumerates the four kinds", () => {
    const kind = (schemas.ResetBody.properties as Record<string, { enum?: string[]; $ref?: string }>).kind;
    const values = kind.enum ?? (schemas[kind.$ref?.split("/").pop() ?? ""] as { enum?: string[] })?.enum;
    expect(values?.sort()).toEqual(["cold", "factory", "hot", "warm"]);
  });

  it("the mode body's mode is the BaseMode enum", () => {
    expect((schemas.BaseMode as { enum?: string[] }).enum?.sort()).toEqual(["fixed", "off", "survey-in"]);
  });
});
