export type Sector =
  | "Semiconductors"
  | "Robotics"
  | "Data centers"
  | "Networking"
  | "Energy";

export interface Company {
  name: string;
  symbol: string;
  sector: Sector;
  focus: string;
  signal: "Building" | "Monitoring" | "Early";
  note: string;
}

export const companies: Company[] = [
  {
    name: "Asterion Foundry",
    symbol: "SMP-01",
    sector: "Semiconductors",
    focus: "Advanced packaging",
    signal: "Building",
    note: "Illustrative capacity expansion",
  },
  {
    name: "Northstar Motion",
    symbol: "ROB-02",
    sector: "Robotics",
    focus: "Warehouse automation",
    signal: "Monitoring",
    note: "Sample deployment watch",
  },
  {
    name: "Pillar Compute",
    symbol: "DCP-03",
    sector: "Data centers",
    focus: "Power-dense campuses",
    signal: "Building",
    note: "Illustrative campus pipeline",
  },
  {
    name: "Meridian Fabric",
    symbol: "NET-04",
    sector: "Networking",
    focus: "Optical interconnect",
    signal: "Early",
    note: "Sample technology inflection",
  },
  {
    name: "Copperline Grid",
    symbol: "ENG-05",
    sector: "Energy",
    focus: "Grid modernization",
    signal: "Monitoring",
    note: "Illustrative transmission buildout",
  },
  {
    name: "Kestrel Silicon",
    symbol: "SMP-06",
    sector: "Semiconductors",
    focus: "Power management",
    signal: "Early",
    note: "Sample supply-chain signal",
  },
  {
    name: "Relay Systems",
    symbol: "ROB-07",
    sector: "Robotics",
    focus: "Industrial inspection",
    signal: "Building",
    note: "Illustrative adoption curve",
  },
];

export const sectors: Sector[] = [
  "Semiconductors",
  "Robotics",
  "Data centers",
  "Networking",
  "Energy",
];

export const mapNodes = [
  { id: "nw", label: "Northwest", x: 155, y: 104, kind: "Compute", load: "Sample node A" },
  { id: "sw", label: "Southwest", x: 218, y: 241, kind: "Energy", load: "Sample node B" },
  { id: "mw", label: "Midwest", x: 369, y: 130, kind: "Network", load: "Sample node C" },
  { id: "tx", label: "Texas", x: 370, y: 260, kind: "Energy", load: "Sample node D" },
  { id: "se", label: "Southeast", x: 516, y: 236, kind: "Compute", load: "Sample node E" },
  { id: "ne", label: "Northeast", x: 561, y: 113, kind: "Network", load: "Sample node F" },
  { id: "ca", label: "Pacific", x: 91, y: 193, kind: "Compute", load: "Sample node G" },
] as const;
