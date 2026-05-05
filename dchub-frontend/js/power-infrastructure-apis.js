/**
 * DC Hub Power Infrastructure API Configuration v2.0
 * ===================================================
 * Comprehensive list of verified working APIs for power infrastructure data
 * 
 * Last updated: January 2026
 * 
 * VERIFIED WORKING SOURCES:
 * -------------------------
 * 1. TRANSMISSION LINES: Multiple working sources
 * 2. SUBSTATIONS: OpenStreetMap Overpass API (HIFLD service deprecated)
 * 3. POWER PLANTS: EIA & EPA sources
 * 4. PIPELINES: DOT/PHMSA working
 * 5. REAL-TIME GRID DATA: GridStatus/EIA
 */

const POWER_INFRASTRUCTURE_CONFIG = {
    
    // =========================================================================
    // TRANSMISSION LINES - VERIFIED WORKING
    // =========================================================================
    transmissionLines: {
        // PRIMARY: FedMaps HIFLD Mirror (Updated August 2025)
        primary: {
            name: 'FedMaps US Electric Power Transmission Lines',
            url: 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0',
            status: 'VERIFIED WORKING',
            lastVerified: '2026-01-18',
            maxRecords: 2000,
            fields: ['VOLTAGE', 'OWNER', 'STATUS', 'VOLT_CLASS', 'SUB_1', 'SUB_2'],
            queryFormat: 'esriGeometryEnvelope',
            spatialRef: 4326,
            notes: 'Best source - national coverage, updated regularly'
        },
        
        // BACKUP: Original HIFLD (geometry format sensitive)
        backup: {
            name: 'HIFLD Electric Power Transmission Lines',
            url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
            status: 'WORKING - REQUIRES CORRECT GEOMETRY FORMAT',
            lastVerified: '2026-01-18',
            maxRecords: 2000,
            fields: ['VOLTAGE', 'OWNER', 'STATUS'],
            notes: 'Requires JSON geometry format, not string envelope'
        },
        
        // REGIONAL: Rutgers MARCO Portal (Northeast US only)
        regional_northeast: {
            name: 'Rutgers HIFLD Transmission Lines',
            url: 'https://oceandata.rad.rutgers.edu/arcgis/rest/services/RenewableEnergy/HIFLD_Electric_SubstationsTransmissionLines/MapServer/1',
            status: 'WORKING - REGIONAL ONLY',
            coverage: ['ME', 'NH', 'MA', 'RI', 'CT', 'NY', 'NJ', 'PA', 'DE', 'MD', 'VA'],
            notes: 'Maine to Virginia only'
        }
    },
    
    // =========================================================================
    // SUBSTATIONS - HIFLD DEPRECATED, USE ALTERNATIVES
    // =========================================================================
    substations: {
        // PRIMARY: OpenStreetMap Overpass API (Global, Real-time)
        primary: {
            name: 'OpenStreetMap Overpass API',
            url: 'https://overpass-api.de/api/interpreter',
            status: 'VERIFIED WORKING',
            lastVerified: '2026-01-18',
            type: 'overpass',
            coverage: 'Global',
            notes: 'Best current source - HIFLD national service deprecated',
            queryTemplate: `
                [out:json][timeout:30];
                (
                    node["power"="substation"]({{bbox}});
                    way["power"="substation"]({{bbox}});
                    relation["power"="substation"]({{bbox}});
                );
                out center;
            `,
            fields: ['name', 'operator', 'voltage', 'substation']
        },
        
        // BACKUP: Rutgers MARCO (Northeast only)
        regional_northeast: {
            name: 'Rutgers HIFLD Electric Substations',
            url: 'https://oceandata.rad.rutgers.edu/arcgis/rest/services/RenewableEnergy/HIFLD_Electric_SubstationsTransmissionLines/MapServer/0',
            status: 'WORKING - REGIONAL ONLY',
            coverage: ['ME', 'NH', 'MA', 'RI', 'CT', 'NY', 'NJ', 'PA', 'DE', 'MD', 'VA'],
            notes: 'Maine to Virginia only - good for Northeast'
        },
        
        // DEPRECATED: Original HIFLD
        deprecated: {
            name: 'HIFLD Electric Substations (DEPRECATED)',
            url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0',
            status: 'DEPRECATED - SERVICE REMOVED',
            notes: 'No longer available as of late 2025'
        }
    },
    
    // =========================================================================
    // POWER PLANTS - MULTIPLE WORKING SOURCES
    // =========================================================================
    powerPlants: {
        // PRIMARY: EIA Power Plants (via ArcGIS)
        primary: {
            name: 'EIA Power Plants',
            url: 'https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services/Power_Plants/FeatureServer/0',
            status: 'NEEDS VERIFICATION',
            fields: ['Plant_Name', 'Utility_Name', 'Sector_Name', 'PrimSource', 'Total_MW'],
            notes: 'EIA-860 data'
        },
        
        // BACKUP: EPA FRS Power Plants
        backup: {
            name: 'EPA FRS Power Plants',
            url: 'https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_PowerPlants/MapServer/15',
            status: 'VERIFIED EXISTS',
            notes: 'EPA Facility Registry Service - EIA-860 + EPA data'
        },
        
        // OpenStreetMap
        osm: {
            name: 'OpenStreetMap Power Plants',
            url: 'https://overpass-api.de/api/interpreter',
            type: 'overpass',
            queryTemplate: `
                [out:json][timeout:30];
                (
                    node["power"="plant"]({{bbox}});
                    way["power"="plant"]({{bbox}});
                    relation["power"="plant"]({{bbox}});
                );
                out center;
            `
        }
    },
    
    // =========================================================================
    // GAS PIPELINES - VERIFIED WORKING
    // =========================================================================
    pipelines: {
        // PRIMARY: DOT Natural Gas Pipelines
        primary: {
            name: 'DOT/PHMSA Natural Gas Pipelines',
            url: 'https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0',
            status: 'VERIFIED WORKING',
            lastVerified: '2026-01-18',
            fields: ['typepipe', 'operator', 'status'],
            notes: 'DOT/EIA pipeline data - interstate and intrastate'
        },
        
        // Texas RRC
        texas: {
            name: 'Texas RRC Pipelines',
            url: 'https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer/0',
            status: 'VERIFIED WORKING',
            coverage: ['TX'],
            fields: ['P5_NUM', 'OPERATOR_NAME', 'PIPELINE_TYPE', 'COMMODITY']
        }
    },
    
    // =========================================================================
    // REAL-TIME GRID DATA
    // =========================================================================
    gridData: {
        // EIA API v2
        eia: {
            name: 'EIA API v2',
            baseUrl: 'https://api.eia.gov/v2',
            endpoints: {
                gridMonitor: '/electricity/rto/daily-region-data/data',
                fuelMix: '/electricity/rto/fuel-type-data/data',
                demand: '/electricity/rto/region-sub-ba-data/data',
                generatorCapacity: '/electricity/operating-generator-capacity/data'
            },
            requiresKey: true,
            keyEnvVar: 'EIA_API_KEY',
            notes: 'Free API key required - register at eia.gov/opendata'
        },
        
        // GridStatus.io
        gridStatus: {
            name: 'GridStatus.io',
            baseUrl: 'https://api.gridstatus.io',
            status: 'COMMERCIAL - FREE TIER AVAILABLE',
            freeLimit: '1M rows/month',
            coverage: ['CAISO', 'ERCOT', 'PJM', 'MISO', 'SPP', 'NYISO', 'ISO-NE'],
            notes: 'Best for real-time ISO data'
        }
    },
    
    // =========================================================================
    // NUCLEAR FACILITIES
    // =========================================================================
    nuclear: {
        // NRC Licensed Facilities
        nrc: {
            name: 'NRC Licensed Facilities',
            url: 'https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/NRC_Nuclear_Reactors/FeatureServer/0',
            status: 'NEEDS VERIFICATION'
        }
    },
    
    // =========================================================================
    // INTERCONNECTION QUEUES
    // =========================================================================
    interconnectionQueues: {
        // Each ISO publishes their own queue data
        sources: {
            PJM: 'https://www.pjm.com/planning/services-requests/interconnection-queues',
            MISO: 'https://www.misoenergy.org/planning/generator-interconnection/',
            CAISO: 'https://rimspub.caiso.com/rims-web/',
            ERCOT: 'https://www.ercot.com/gridinfo/resource',
            SPP: 'https://www.spp.org/engineering/generator-interconnection/',
            NYISO: 'https://www.nyiso.com/interconnections',
            ISONE: 'https://www.iso-ne.com/system-planning/transmission-planning/interconnection-request-queue'
        },
        notes: 'Queue data typically available as downloadable spreadsheets, not APIs'
    }
};

// =========================================================================
// QUERY HELPERS
// =========================================================================

/**
 * Build ArcGIS envelope query with correct format
 */
function buildArcGISQuery(baseUrl, bounds, options = {}) {
    const {
        outFields = '*',
        maxRecords = 1000,
        spatialRef = 4326
    } = options;
    
    // Use JSON geometry format (works with more services)
    const geometry = JSON.stringify({
        xmin: bounds.west,
        ymin: bounds.south,
        xmax: bounds.east,
        ymax: bounds.north,
        spatialReference: { wkid: spatialRef }
    });
    
    const params = new URLSearchParams({
        where: '1=1',
        geometry: geometry,
        geometryType: 'esriGeometryEnvelope',
        inSR: spatialRef,
        spatialRel: 'esriSpatialRelIntersects',
        outFields: outFields,
        f: 'json',
        resultRecordCount: maxRecords
    });
    
    return `${baseUrl}/query?${params}`;
}

/**
 * Build Overpass API query for substations
 */
function buildOverpassSubstationQuery(bounds) {
    const bbox = `${bounds.south},${bounds.west},${bounds.north},${bounds.east}`;
    
    return `
        [out:json][timeout:30];
        (
            node["power"="substation"](${bbox});
            way["power"="substation"](${bbox});
            relation["power"="substation"](${bbox});
        );
        out center;
    `;
}

/**
 * Fetch substations from OpenStreetMap
 */
async function fetchOSMSubstations(bounds) {
    const query = buildOverpassSubstationQuery(bounds);
    
    const response = await fetch('https://overpass-api.de/api/interpreter', {
        method: 'POST',
        body: `data=${encodeURIComponent(query)}`,
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    });
    
    const data = await response.json();
    
    // Transform OSM format to standard format
    return data.elements.map(el => ({
        id: el.id,
        type: el.type,
        name: el.tags?.name || 'Unknown',
        operator: el.tags?.operator || 'Unknown',
        voltage: el.tags?.voltage || 'Unknown',
        substation_type: el.tags?.substation || 'transmission',
        lat: el.lat || el.center?.lat,
        lng: el.lon || el.center?.lon,
        source: 'OpenStreetMap'
    }));
}

/**
 * Fetch transmission lines from FedMaps
 */
async function fetchTransmissionLines(bounds) {
    const url = buildArcGISQuery(
        POWER_INFRASTRUCTURE_CONFIG.transmissionLines.primary.url,
        bounds,
        { outFields: 'VOLTAGE,OWNER,STATUS,VOLT_CLASS,SUB_1,SUB_2', maxRecords: 2000 }
    );
    
    const response = await fetch(url);
    const data = await response.json();
    
    return data.features || [];
}

/**
 * Fetch gas pipelines from DOT
 */
async function fetchGasPipelines(bounds) {
    const url = buildArcGISQuery(
        POWER_INFRASTRUCTURE_CONFIG.pipelines.primary.url,
        bounds,
        { outFields: 'typepipe,operator,status', maxRecords: 1000 }
    );
    
    const response = await fetch(url);
    const data = await response.json();
    
    return data.features || [];
}

// =========================================================================
// EXPORT
// =========================================================================

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        POWER_INFRASTRUCTURE_CONFIG,
        buildArcGISQuery,
        buildOverpassSubstationQuery,
        fetchOSMSubstations,
        fetchTransmissionLines,
        fetchGasPipelines
    };
}

if (typeof window !== 'undefined') {
    window.PowerInfrastructureConfig = POWER_INFRASTRUCTURE_CONFIG;
    window.PowerInfrastructureHelpers = {
        buildArcGISQuery,
        buildOverpassSubstationQuery,
        fetchOSMSubstations,
        fetchTransmissionLines,
        fetchGasPipelines
    };
}

console.log('⚡ Power Infrastructure API Config v2.0 loaded');
console.log('   Working sources: Transmission (FedMaps), Substations (OSM), Pipelines (DOT)');
