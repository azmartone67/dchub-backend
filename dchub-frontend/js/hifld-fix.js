/**
 * HIFLD Substations Query Fix
 * ============================
 * The current query returns 0 substations because the field names or 
 * service URL may have changed. This patch provides updated endpoints.
 * 
 * Apply this fix by updating the HIFLD query in dchub-infrastructure.js
 */

// ============================================
// WORKING HIFLD ENDPOINTS (as of Jan 2025)
// ============================================

const HIFLD_ENDPOINTS = {
    // Electric Substations - PRIMARY (ArcGIS Feature Service)
    substations: {
        url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query',
        fields: 'NAME,CITY,STATE,ZIP,STATUS,LINES,MAX_VOLT,MIN_VOLT,LATITUDE,LONGITUDE',
        // Note: Capacity (MW) field is 'MAX_VOLT' in this dataset, not direct MW
    },
    
    // Electric Power Transmission Lines
    transmission: {
        url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query',
        fields: 'ID,OWNER,VOLTAGE,VOLT_CLASS,SHAPE_Length',
    },
    
    // Power Plants
    powerPlants: {
        url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0/query',
        fields: 'NAME,CITY,STATE,ZIP,PRIMESOURCE,TOTAL_MW,STATUS,LATITUDE,LONGITUDE',
    },
    
    // Natural Gas Pipelines
    gasPipelines: {
        url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0/query',
        fields: 'TYPEPIPE,OPERATOR,Shape_Length',
    },
    
    // Natural Gas Compressor Stations
    gasCompressors: {
        url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query',
        fields: 'NAME,OPERATOR,STATE,LATITUDE,LONGITUDE',
    }
};

/**
 * Fetch HIFLD substations with proper parameters
 */
async function fetchHIFLDSubstations(bounds, options = {}) {
    const sw = bounds.getSouthWest();
    const ne = bounds.getNorthEast();
    
    // Build geometry envelope
    const geometry = {
        xmin: sw.lng,
        ymin: sw.lat,
        xmax: ne.lng,
        ymax: ne.lat,
        spatialReference: { wkid: 4326 }
    };
    
    const params = new URLSearchParams({
        where: '1=1',
        geometry: JSON.stringify(geometry),
        geometryType: 'esriGeometryEnvelope',
        inSR: '4326',
        outSR: '4326',
        spatialRel: 'esriSpatialRelIntersects',
        outFields: HIFLD_ENDPOINTS.substations.fields,
        returnGeometry: 'true',
        resultRecordCount: options.limit || 500,
        f: 'json'
    });
    
    try {
        const response = await fetch(`${HIFLD_ENDPOINTS.substations.url}?${params}`);
        const data = await response.json();
        
        if (data.error) {
            console.error('HIFLD API Error:', data.error);
            return [];
        }
        
        if (data.features && data.features.length > 0) {
            console.log(`🔌 HIFLD: Loaded ${data.features.length} substations`);
            return data.features.map(f => ({
                name: f.attributes.NAME || 'Unknown',
                city: f.attributes.CITY,
                state: f.attributes.STATE,
                voltage_kv: f.attributes.MAX_VOLT || 0,
                min_voltage_kv: f.attributes.MIN_VOLT || 0,
                lines: f.attributes.LINES || 0,
                status: f.attributes.STATUS,
                lat: f.attributes.LATITUDE || (f.geometry && f.geometry.y),
                lng: f.attributes.LONGITUDE || (f.geometry && f.geometry.x),
                source: 'HIFLD'
            }));
        }
        
        return [];
        
    } catch (error) {
        console.error('HIFLD fetch error:', error);
        return [];
    }
}

/**
 * Fetch HIFLD transmission lines
 */
async function fetchHIFLDTransmission(bounds, options = {}) {
    const sw = bounds.getSouthWest();
    const ne = bounds.getNorthEast();
    
    const geometry = {
        xmin: sw.lng,
        ymin: sw.lat,
        xmax: ne.lng,
        ymax: ne.lat,
        spatialReference: { wkid: 4326 }
    };
    
    const params = new URLSearchParams({
        where: '1=1',
        geometry: JSON.stringify(geometry),
        geometryType: 'esriGeometryEnvelope',
        inSR: '4326',
        outSR: '4326',
        spatialRel: 'esriSpatialRelIntersects',
        outFields: HIFLD_ENDPOINTS.transmission.fields,
        returnGeometry: 'true',
        resultRecordCount: options.limit || 200,
        f: 'json'
    });
    
    try {
        const response = await fetch(`${HIFLD_ENDPOINTS.transmission.url}?${params}`);
        const data = await response.json();
        
        if (data.features && data.features.length > 0) {
            console.log(`⚡ HIFLD: Loaded ${data.features.length} transmission lines`);
            return data.features;
        }
        
        return [];
        
    } catch (error) {
        console.error('HIFLD transmission fetch error:', error);
        return [];
    }
}

/**
 * Fetch HIFLD power plants
 */
async function fetchHIFLDPowerPlants(bounds, options = {}) {
    const sw = bounds.getSouthWest();
    const ne = bounds.getNorthEast();
    
    const geometry = {
        xmin: sw.lng,
        ymin: sw.lat,
        xmax: ne.lng,
        ymax: ne.lat,
        spatialReference: { wkid: 4326 }
    };
    
    const params = new URLSearchParams({
        where: 'TOTAL_MW > 0', // Only plants with capacity
        geometry: JSON.stringify(geometry),
        geometryType: 'esriGeometryEnvelope',
        inSR: '4326',
        outSR: '4326',
        spatialRel: 'esriSpatialRelIntersects',
        outFields: HIFLD_ENDPOINTS.powerPlants.fields,
        returnGeometry: 'true',
        resultRecordCount: options.limit || 200,
        f: 'json'
    });
    
    try {
        const response = await fetch(`${HIFLD_ENDPOINTS.powerPlants.url}?${params}`);
        const data = await response.json();
        
        if (data.features && data.features.length > 0) {
            console.log(`🏭 HIFLD: Loaded ${data.features.length} power plants`);
            return data.features.map(f => ({
                name: f.attributes.NAME || 'Unknown Plant',
                city: f.attributes.CITY,
                state: f.attributes.STATE,
                fuel_type: f.attributes.PRIMESOURCE,
                capacity_mw: f.attributes.TOTAL_MW || 0,
                status: f.attributes.STATUS,
                lat: f.attributes.LATITUDE || (f.geometry && f.geometry.y),
                lng: f.attributes.LONGITUDE || (f.geometry && f.geometry.x),
                source: 'HIFLD'
            }));
        }
        
        return [];
        
    } catch (error) {
        console.error('HIFLD power plants fetch error:', error);
        return [];
    }
}

// Export for use
window.HIFLDFix = {
    endpoints: HIFLD_ENDPOINTS,
    fetchSubstations: fetchHIFLDSubstations,
    fetchTransmission: fetchHIFLDTransmission,
    fetchPowerPlants: fetchHIFLDPowerPlants
};

console.log('🔌 HIFLD Query Fix loaded - use HIFLDFix.fetchSubstations(bounds)');
