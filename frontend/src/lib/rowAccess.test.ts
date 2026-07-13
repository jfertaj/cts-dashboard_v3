import { describe, it, expect } from "vitest";
import { readDataCell } from "./rowAccess";

describe("readDataCell", () => {
  it("lee la clave exacta tal cual la manda el backend", () => {
    const row = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": 240 } };
    expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBe(240);
  });

  it("cae a la clave sin el prefijo sf.", () => {
    const row = { data: { C_Number_of_Stage1_Individuals_followed__c: 8 } };
    expect(readDataCell(row, "sf.C_Number_of_Stage1_Individuals_followed__c")).toBe(8);
  });

  it("mapea sf.Account.Name al campo plano account_name", () => {
    const row = { account_name: "AOU Careggi_CS-Ad", data: {} };
    expect(readDataCell(row, "sf.Account.Name")).toBe("AOU Careggi_CS-Ad");
  });

  it("devuelve undefined cuando la clave no existe en ninguna variante", () => {
    const row = { data: {} };
    expect(readDataCell(row, "sf.No_Existe__c")).toBeUndefined();
  });

  it("trata la cadena vacía como ausente, no como valor", () => {
    const row = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": "" } };
    expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBeUndefined();
  });
});
