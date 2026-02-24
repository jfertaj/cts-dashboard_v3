# 🔴 REGLA DE ORO PARA MOBY

## Regla Simple y Clara

```
┌─────────────────────────────────────────────────────────┐
│  Use Postgres (sql_query) SOLO para site_qual          │
│  Todo lo demás usa Salesforce                           │
└─────────────────────────────────────────────────────────┘
```

## ✅ Usa Postgres SOLO Para:

1. **Preguntas de qualification checklist**:
   - "Sites with overnight stay facilities"
   - "Sites with onsite pharmacy"
   - "Sites with examination rooms"
   - Cualquier campo que venga de los archivos Excel de qualification

2. **Búsqueda en comentarios de qualification**:
   - "Search comments for power outage"
   - "Find sites mentioning X in qualification"

**Eso es TODO.** Nada más debe usar Postgres.

## ✅ Usa Salesforce Para TODO LO DEMÁS:

### Sitios
- ❌ ~~sql_query~~ → ✅ **salesforce_query Account**
- "How many sites per country?"
- "Show all sites in Spain"
- "Total number of sites"
- "Sites per city"

### Geografía
- ❌ ~~sites.country, sites.city~~ → ✅ **Account.ShippingCountry, Account.ShippingCity**

### Pacientes
- ❌ ~~sql_query~~ → ✅ **salesforce_query Opportunity**
- "T1D patients under 18"
- "Newly diagnosed patients"
- "Stage 1/Stage 2 individuals"

### Contactos
- ❌ ~~sql_query~~ → ✅ **salesforce_account_contacts**
- "Show me the PI"
- "Study Coordinators in Belgium"
- "All contacts with role X"

### Trials/Assignments
- ❌ ~~sql_query~~ → ✅ **salesforce_query Opportunity / salesforce_assignments**

## Por Qué Esta Regla

**Postgres `sites` table**:
- ❌ **Incompleta**: Solo contiene sitios que subieron archivos de qualification
- ❌ **No es fuente de verdad**: Puede estar desactualizada
- ✅ **Solo para**: Datos de los Excel de qualification (site_qual)

**Salesforce Account**:
- ✅ **Completa**: Tiene TODOS los sitios activos
- ✅ **Fuente de verdad**: Datos oficiales y actualizados
- ✅ **Para todo**: Listas, conteos, geografía, enlaces

## Ejemplos Correctos

### ✅ CORRECTO: Sites per country
```python
# Tool: salesforce_query
SOQL: SELECT ShippingCountry country, COUNT(Id) sites 
      FROM Account 
      WHERE RecordType.DeveloperName = 'SubAccount' 
        AND C_Type__c = 'Clinical'
        AND ShippingCountry != null
        AND (Account_Inactive__c = false OR Account_Inactive__c = null)
        AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)
      GROUP BY ShippingCountry 
      ORDER BY COUNT(Id) DESC
```

### ✅ CORRECTO: Sites with pharmacy
```python
# Tool: sql_query
SQL: SELECT sites.salesforce_account_id, sites.name, 
            sq.data->>'onsite_pharmacy' as pharmacy
     FROM sites 
     JOIN site_qual sq ON sq.site_id = sites.id
     WHERE sq.data->>'onsite_pharmacy' IS NOT NULL
```

### ❌ INCORRECTO: Sites per country
```python
# ❌ NO HACER ESTO
# Tool: sql_query
SQL: SELECT country, COUNT(*) FROM sites GROUP BY country
# PROBLEMA: sites table está incompleta
```

## Implementación Técnica

### System Prompt
- Regla de oro en CRITICAL ROUTING RULES
- Ejemplos claros en BLOCK 1, 2, 3
- Tool reference simplificado

### Fast Planner
- Detecta queries básicas de sitios
- Las envía al LLM con hint explícito
- NO ejecuta `group_count` para conteos básicos

### Routing Hints
- Detecta automáticamente tipo de query
- Añade mensaje 🔴 CRITICAL ROUTING cuando es necesario
- Recuerda la regla en cada query: "Postgres ONLY for site_qual"

## Testing

Prueba estas queries para verificar:

✅ **Debe usar Salesforce**:
- "How many sites per country?"
- "Show sites in Spain"
- "Total sites"
- "Who is the PI for site X?"
- "Sites with most T1D patients"

✅ **Debe usar Postgres**:
- "Sites with overnight stay"
- "Sites with onsite pharmacy"
- "Search comments for X"

## Deployment Status

- ✅ System prompt actualizado con regla de oro
- ✅ Fast planner deshabilitado para conteos básicos
- ✅ Routing hints añadidos para forzar Salesforce
- ✅ Backend reconstruido y reiniciado
- ✅ Cambios activos desde: 2025-10-31 20:15 UTC

## Archivos Relacionados

- `backend/app/routers/ai_chat.py` - Toda la lógica de routing
- `FIX_SITE_COUNT_ROUTING.md` - Documentación detallada del fix
- `CHART_IMPROVEMENTS.md` - Mejoras de gráficos de sesión anterior
