import { describe, it, expect } from "vitest";
import { readDataCell, type DataRow } from "./rowAccess";

describe("readDataCell", () => {
  describe("capa 1 — clave exacta", () => {
    it("lee la clave exacta tal cual la manda el backend", () => {
      const row: DataRow = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": 240 } };
      expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBe(240);
    });

    it("devuelve el 0 numérico: es un valor presente, no un hueco", () => {
      const row: DataRow = { data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 0 } };
      expect(readDataCell(row, "sf.C_Number_of_Stage1_Individuals_followed__c")).toBe(0);
    });
  });

  describe("capa 2 — clave sin el prefijo sf.", () => {
    it("cae a la clave sin el prefijo sf.", () => {
      const row: DataRow = { data: { C_Number_of_Stage1_Individuals_followed__c: 8 } };
      expect(readDataCell(row, "sf.C_Number_of_Stage1_Individuals_followed__c")).toBe(8);
    });

    it("cae a la clave sin prefijo aunque conserve puntos, antes de probar los underscores", () => {
      // Con base "Account.Name" las capas de underscore buscan otras claves distintas,
      // así que este caso sólo lo puede resolver la capa 2.
      const row: DataRow = { account_name: "el campo plano", data: { "Account.Name": "el valor de la capa 2" } };
      expect(readDataCell(row, "sf.Account.Name")).toBe("el valor de la capa 2");
    });
  });

  describe("capa 3 — variantes con underscores", () => {
    it("cae a k2: la clave entera con los puntos convertidos en underscores", () => {
      const row: DataRow = { data: { "sf_C_Number_of_Individuals_screened_intotal__c": 15 } };
      expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBe(15);
    });

    it("cae a k3: la clave SIN prefijo sf. y con los puntos en underscores", () => {
      // "sf.Account.Name" -> base "Account.Name" -> k3 "Account_Name".
      // account_name lleva otro valor: si respondiera el campo plano en vez de k3, se vería.
      const row: DataRow = { account_name: "el campo plano", data: { Account_Name: "el valor de k3" } };
      expect(readDataCell(row, "sf.Account.Name")).toBe("el valor de k3");
    });
  });

  describe("capa 4 — fallbacks a los campos planos de la fila", () => {
    it("mapea sf.Account.Name al campo plano account_name", () => {
      const row: DataRow = { account_name: "AOU Careggi_CS-Ad", data: {} };
      expect(readDataCell(row, "sf.Account.Name")).toBe("AOU Careggi_CS-Ad");
    });

    it("mapea Account.Name (sin prefijo sf.) al campo plano account_name", () => {
      const row: DataRow = { account_name: "AOU Careggi_CS-Ad", data: {} };
      expect(readDataCell(row, "Account.Name")).toBe("AOU Careggi_CS-Ad");
    });

    it("mapea sf.Account.Id al campo plano account_id", () => {
      const row: DataRow = { account_id: "001Aa00000ABCDEfgh", data: {} };
      expect(readDataCell(row, "sf.Account.Id")).toBe("001Aa00000ABCDEfgh");
    });

    it("mapea Account.Id (sin prefijo sf.) al campo plano account_id", () => {
      const row: DataRow = { account_id: "001Aa00000ABCDEfgh", data: {} };
      expect(readDataCell(row, "Account.Id")).toBe("001Aa00000ABCDEfgh");
    });

    it("mapea country al campo plano country", () => {
      const row: DataRow = { country: "ES", data: {} };
      expect(readDataCell(row, "country")).toBe("ES");
    });

    it("mapea sf.Account.ShippingCountry al campo plano country", () => {
      const row: DataRow = { country: "IT", data: {} };
      expect(readDataCell(row, "sf.Account.ShippingCountry")).toBe("IT");
    });

    it("mapea city al campo plano city", () => {
      const row: DataRow = { city: "Firenze", data: {} };
      expect(readDataCell(row, "city")).toBe("Firenze");
    });

    it("mapea sf.Account.ShippingCity al campo plano city", () => {
      const row: DataRow = { city: "Leuven", data: {} };
      expect(readDataCell(row, "sf.Account.ShippingCity")).toBe("Leuven");
    });

    it("resuelve los campos planos ignorando mayúsculas/minúsculas de la clave", () => {
      const row: DataRow = { country: "BE", data: {} };
      expect(readDataCell(row, "sf.ACCOUNT.SHIPPINGCOUNTRY")).toBe("BE");
    });

    it("devuelve undefined si el campo plano al que mapea la clave no está en la fila", () => {
      const row: DataRow = { data: {} };
      expect(readDataCell(row, "sf.Account.Name")).toBeUndefined();
    });
  });

  describe("un valor vacío cuenta como ausente y la búsqueda continúa", () => {
    it("trata la cadena vacía como ausente, no como valor", () => {
      const row: DataRow = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": "" } };
      expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBeUndefined();
    });

    it("una cadena vacía en la clave exacta deja pasar a la clave sin prefijo sf.", () => {
      const row: DataRow = {
        data: {
          "sf.C_Number_of_Stage1_Individuals_followed__c": "",
          C_Number_of_Stage1_Individuals_followed__c: 8,
        },
      };
      expect(readDataCell(row, "sf.C_Number_of_Stage1_Individuals_followed__c")).toBe(8);
    });

    it("null y espacios en blanco también dejan pasar: cae hasta k2 con el valor real", () => {
      const row: DataRow = {
        data: {
          "sf.C_Number_of_Individuals_screened_intotal__c": null,
          C_Number_of_Individuals_screened_intotal__c: "   ",
          "sf_C_Number_of_Individuals_screened_intotal__c": 240,
        },
      };
      expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBe(240);
    });

    it("las cuatro capas vacías dejan pasar hasta el campo plano de la fila", () => {
      const row: DataRow = {
        account_name: "AOU Careggi_CS-Ad",
        data: {
          "sf.Account.Name": "",
          "Account.Name": null,
          sf_Account_Name: "   ",
          Account_Name: "",
        },
      };
      expect(readDataCell(row, "sf.Account.Name")).toBe("AOU Careggi_CS-Ad");
    });
  });

  it("devuelve undefined cuando la clave no existe en ninguna variante", () => {
    const row: DataRow = { data: {} };
    expect(readDataCell(row, "sf.No_Existe__c")).toBeUndefined();
  });
});
