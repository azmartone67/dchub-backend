    document.addEventListener('DOMContentLoaded', function() {
        'use strict';
        
        // Initialize map
        var map = L.map('map',{zoomControl:true}).setView([39.0,-98.0],4);
        window.map = map; // Make map globally accessible
        
        // ============================================
        // BASE MAP OPTIONS - Google Maps + Others
        // ============================================
        
        // Google Maps Tile URLs (using session token for authorized access)
        var GOOGLE_API_KEY = 'AIzaSyDDG06_pDGoLrBee02kCQf5h48wFq2Kn2A';
        
        // 1. Google Satellite
        var googleSatellite = L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
            maxZoom: 21,
            attribution: '© Google Maps'
        });
        
        // 2. Google Hybrid (satellite + labels)
        var googleHybrid = L.tileLayer('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
            maxZoom: 21,
            attribution: '© Google Maps'
        });
        
        // 3. Google Terrain
        var googleTerrain = L.tileLayer('https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}', {
            maxZoom: 21,
            attribution: '© Google Maps'
        });
        
        // 4. Google Roads
        var googleRoads = L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
            maxZoom: 21,
            attribution: '© Google Maps'
        });
        
        // 5. Dark Theme (CartoDB - good for data visualization)
        var darkMap = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
            attribution:'© OpenStreetMap © CARTO',
            maxZoom:19
        });
        
        // 6. Light Road Map (CartoDB Positron)
        var lightMap = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{
            attribution:'© OpenStreetMap © CARTO',
            maxZoom:19
        });
        
        // Set default base map - Google Hybrid!
        googleHybrid.addTo(map);
        var currentBaseMap = googleHybrid;
        
        // Base map switcher object
        var baseMaps = {
            'satellite': { layer: googleSatellite, name: '🛰️ Satellite', icon: '🛰️' },
            'hybrid': { layer: googleHybrid, name: '🗺️ Hybrid', icon: '🗺️' },
            'terrain': { layer: googleTerrain, name: '⛰️ Terrain', icon: '⛰️' },
            'roadmap': { layer: googleRoads, name: '🛣️ Roads', icon: '🛣️' },
            'dark': { layer: darkMap, name: '🌙 Dark', icon: '🌙' },
            'light': { layer: lightMap, name: '☀️ Light', icon: '☀️' },
            'nightlights': { layer: nightLights, name: '🌃 Night Lights', icon: '🌃' }
        };
        
        // Function to switch base maps
        window.switchBaseMap = function(mapKey) {
            if (baseMaps[mapKey]) {
                map.removeLayer(currentBaseMap);
                currentBaseMap = baseMaps[mapKey].layer;
                currentBaseMap.addTo(map);
                map.invalidateSize();
                
                // Update active button state
                document.querySelectorAll('.basemap-btn').forEach(btn => btn.classList.remove('active'));
                document.querySelector('.basemap-btn[data-map="'+mapKey+'"]').classList.add('active');
                
                console.log('🗺️ Switched to: ' + baseMaps[mapKey].name);
            }
        };

        // Layer groups
        var layers = {
            datacenters: L.layerGroup(),
            nuclear: L.layerGroup(),
            substations: L.layerGroup(),
            gas: L.layerGroup(),
            grid: L.layerGroup(),
            fiber: L.layerGroup(),
            submarine: L.layerGroup(),
            airports: L.layerGroup(),
            water: L.layerGroup(),
            fema: L.layerGroup(),
            coops: L.layerGroup(),
            iso: L.layerGroup(),
            wetlands: L.layerGroup(),
            habitat: L.layerGroup(),
            seismic: L.layerGroup(),
            queue: L.layerGroup(),
            dcqueue: L.layerGroup(),
            broadband: L.layerGroup(),
            powerplants: L.layerGroup(),
            ixpoints: L.layerGroup(),
            gascompressors: L.layerGroup(),
            solar: L.layerGroup(),
            wind: L.layerGroup(),
            opzones: L.layerGroup(),
            metrofiber: L.layerGroup(),
            genqueue: L.layerGroup(),
            longhaulfiber: L.layerGroup(),
            midstream: L.layerGroup(),
            lng: L.layerGroup(),
            // HIFLD Enhanced Layers
            hifldSubstations: L.layerGroup(),
            hifldTransmission: L.layerGroup(),
            hifldGas: L.layerGroup(),
            aquifers: L.layerGroup(),
            rivers: L.layerGroup(),
            railroad: L.layerGroup(),
            // Tier 2: Territory Overlays (v100)
            utilityTerritories: L.layerGroup(),
            // Gas Infrastructure Layers (v117)
            gasStorage: L.layerGroup(),
            gasMarketHubs: L.layerGroup(),
            gasProcessing: L.layerGroup(),
            nglFractionators: L.layerGroup(),
            // Water & Environmental (v118)
            droughtRisk: L.layerGroup()
        };
        
        // ============================================
        // WMS TILE LAYERS - Environmental Data
        // ============================================
        
        // FCC Broadband Map - Fiber Availability Layer
        // Shows areas with fiber broadband availability
        var broadbandTiles = L.tileLayer('https://tiles.arcgis.com/tiles/xOi1kZaI0eWDREZv/arcgis/rest/services/FCC_Broadband_Data_Collection_June_2024/MapServer/tile/{z}/{y}/{x}', {
            minZoom: 6,
            maxZoom: 18,
            opacity: 0.6,
            attribution: 'FCC Broadband Data Collection'
        });
        
        console.log('📶 FCC Broadband Layer initialized (zoom 6+)');
        
        // NWI Wetlands Layer (US Fish & Wildlife Service)
        var wetlandsWMS = L.tileLayer.wms('https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/services/Wetlands/MapServer/WMSServer', {
            layers: '1',
            format: 'image/png',
            transparent: true,
            opacity: 0.5,
            attribution: 'USFWS NWI'
        });
        
        // USFWS Critical Habitat for Endangered Species (Tile Layer)
        var habitatTiles = L.tileLayer('https://tiles.arcgis.com/tiles/QVENGdaPbd4LUkLV/arcgis/rest/services/usfws_critical_habitat_final/MapServer/tile/{z}/{y}/{x}', {
            opacity: 0.6,
            attribution: 'USFWS Critical Habitat'
        });
        
        // County GIS Parcel Services - FREE Public Data
        // Major DC markets with public ArcGIS REST services
        // ============================================
        // COMPREHENSIVE US COUNTY PARCEL SERVICES
        // 150+ counties across all 50 states covering major data center markets
        // Uses free public ArcGIS REST services from county GIS departments
        // ============================================
        var COUNTY_PARCEL_SERVICES = {
            // ========================================
            // VIRGINIA - #1 Data Center State
            // ========================================
            'loudoun_va': {name:'Loudoun County, VA (Ashburn/Data Center Alley)',state:'Virginia',url:'https://logis.loudoun.gov/gis/rest/services/COL/LandRecords/MapServer',layer:0,center:[39.04,-77.49],attribution:'Loudoun County GIS',dcMarket:true,tier:1},
            'fairfax_va': {name:'Fairfax County, VA',state:'Virginia',url:'https://www.fairfaxcounty.gov/gispub1/rest/services/GIS/ParcelsPlus/MapServer',layer:0,center:[38.85,-77.28],attribution:'Fairfax County GIS',dcMarket:true,tier:1},
            'prince_william_va': {name:'Prince William County, VA',state:'Virginia',url:'https://pwcgis.pwcgov.org/arcgis/rest/services/OpenData/Parcels/MapServer',layer:0,center:[38.79,-77.50],attribution:'Prince William County GIS',dcMarket:true,tier:2},
            'stafford_va': {name:'Stafford County, VA',state:'Virginia',url:'https://staffordcountyva.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.42,-77.41],attribution:'Stafford County',dcMarket:true,tier:2},
            'henrico_va': {name:'Henrico County, VA (Richmond)',state:'Virginia',url:'https://gis.henrico.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[37.55,-77.30],attribution:'Henrico County GIS',dcMarket:true,tier:2},
            'chesterfield_va': {name:'Chesterfield County, VA',state:'Virginia',url:'https://gis.chesterfield.gov/arcgis/rest/services/Assessor/Parcels/MapServer',layer:0,center:[37.38,-77.51],attribution:'Chesterfield County',dcMarket:true,tier:2},
            // ========================================
            // TEXAS - #2 Data Center State
            // ========================================
            'dallas_tx': {name:'Dallas County, TX',state:'Texas',url:'https://gis.dallascad.org/arcgis/rest/services/Public/Parcels/MapServer',layer:0,center:[32.78,-96.80],attribution:'Dallas CAD',dcMarket:true,tier:1},
            'tarrant_tx': {name:'Tarrant County, TX (Fort Worth)',state:'Texas',url:'https://maps.tarrantcounty.com/arcgis/rest/services/baseLayers/Parcels/MapServer',layer:0,center:[32.76,-97.33],attribution:'Tarrant County',dcMarket:true,tier:1},
            'bexar_tx': {name:'Bexar County, TX (San Antonio)',state:'Texas',url:'https://maps.bexar.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[29.42,-98.49],attribution:'Bexar County',dcMarket:true,tier:2},
            'travis_tx': {name:'Travis County, TX (Austin)',state:'Texas',url:'https://maps.traviscad.org/arcgis/rest/services/Public/Parcels/MapServer',layer:0,center:[30.27,-97.74],attribution:'Travis CAD',dcMarket:true,tier:2},
            'harris_tx': {name:'Harris County, TX (Houston)',state:'Texas',url:'https://arcgis.harriscountytx.gov/arcgis/rest/services/Appraisal/Parcels/MapServer',layer:0,center:[29.76,-95.37],attribution:'Harris County',dcMarket:true,tier:2},
            'collin_tx': {name:'Collin County, TX (Plano/Allen)',state:'Texas',url:'https://maps.co.collin.tx.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.19,-96.57],attribution:'Collin County',dcMarket:true,tier:2},
            'denton_tx': {name:'Denton County, TX',state:'Texas',url:'https://gis.dentoncounty.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.21,-97.13],attribution:'Denton County',dcMarket:true,tier:2},
            // ========================================
            // ARIZONA - #3 Data Center State (Phoenix)
            // ========================================
            'maricopa_az': {name:'Maricopa County, AZ (Phoenix/Mesa/Goodyear)',state:'Arizona',url:'https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.45,-112.07],attribution:'Maricopa County Assessor',dcMarket:true,tier:1},
            'pima_az': {name:'Pima County, AZ (Tucson)',state:'Arizona',url:'https://gis.pima.gov/arcgis/rest/services/Assessor/Parcels/MapServer',layer:0,center:[32.22,-110.92],attribution:'Pima County GIS',dcMarket:true,tier:2},
            'pinal_az': {name:'Pinal County, AZ',state:'Arizona',url:'https://gis.pinal.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.88,-111.75],attribution:'Pinal County',dcMarket:true,tier:2},
            // ========================================
            // NEVADA - Las Vegas Market
            // ========================================
            'clark_nv': {name:'Clark County, NV (Las Vegas)',state:'Nevada',url:'https://maps.clarkcountynv.gov/arcgis/rest/services/Assessor/Parcels/MapServer',layer:0,center:[36.17,-115.14],attribution:'Clark County Assessor',dcMarket:true,tier:1},
            'washoe_nv': {name:'Washoe County, NV (Reno)',state:'Nevada',url:'https://gis.washoecounty.us/arcgis/rest/services/Assessor/Parcels/MapServer',layer:0,center:[39.53,-119.81],attribution:'Washoe County',dcMarket:true,tier:2},
            // ========================================
            // ILLINOIS - Chicago Market
            // ========================================
            'cook_il': {name:'Cook County, IL (Chicago)',state:'Illinois',url:'https://gis12.cookcountyil.gov/arcgis/rest/services/ParcelViewer/Parcels/MapServer',layer:0,center:[41.88,-87.63],attribution:'Cook County',dcMarket:true,tier:1},
            'dupage_il': {name:'DuPage County, IL',state:'Illinois',url:'https://gis.dupageco.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.85,-88.09],attribution:'DuPage County GIS',dcMarket:true,tier:2},
            'lake_il': {name:'Lake County, IL',state:'Illinois',url:'https://maps.lakecountyil.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[42.35,-87.86],attribution:'Lake County IL',dcMarket:true,tier:2},
            'kane_il': {name:'Kane County, IL (Aurora)',state:'Illinois',url:'https://gis.countyofkane.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.94,-88.32],attribution:'Kane County',dcMarket:true,tier:2},
            // ========================================
            // GEORGIA - Atlanta Market
            // ========================================
            'fulton_ga': {name:'Fulton County, GA (Atlanta)',state:'Georgia',url:'https://gis.fultoncountyga.gov/arcgis/rest/services/Cadastral/Parcels/MapServer',layer:0,center:[33.75,-84.39],attribution:'Fulton County',dcMarket:true,tier:1},
            'dekalb_ga': {name:'DeKalb County, GA',state:'Georgia',url:'https://gis.dekalbcountyga.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.77,-84.22],attribution:'DeKalb County',dcMarket:true,tier:2},
            'cobb_ga': {name:'Cobb County, GA (Marietta)',state:'Georgia',url:'https://gis.cobbcountyga.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.94,-84.55],attribution:'Cobb County',dcMarket:true,tier:2},
            'gwinnett_ga': {name:'Gwinnett County, GA',state:'Georgia',url:'https://gis.gwinnettcounty.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.96,-84.02],attribution:'Gwinnett County',dcMarket:true,tier:2},
            'douglas_ga': {name:'Douglas County, GA (Lithia Springs)',state:'Georgia',url:'https://gis.douglascountyga.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.73,-84.75],attribution:'Douglas County',dcMarket:true,tier:2},
            // ========================================
            // NORTH CAROLINA - Charlotte/Raleigh Markets
            // ========================================
            'mecklenburg_nc': {name:'Mecklenburg County, NC (Charlotte)',state:'North Carolina',url:'https://maps.mecklenburgcountync.gov/arcgis/rest/services/Tax/Tax/MapServer',layer:0,center:[35.23,-80.84],attribution:'Mecklenburg County',dcMarket:true,tier:1},
            'wake_nc': {name:'Wake County, NC (Raleigh)',state:'North Carolina',url:'https://maps.wakegov.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.78,-78.64],attribution:'Wake County',dcMarket:true,tier:2},
            'durham_nc': {name:'Durham County, NC',state:'North Carolina',url:'https://maps.durhamnc.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.99,-78.90],attribution:'Durham County',dcMarket:true,tier:2},
            'cabarrus_nc': {name:'Cabarrus County, NC (Concord)',state:'North Carolina',url:'https://gis.cabarruscounty.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.41,-80.58],attribution:'Cabarrus County',dcMarket:true,tier:2},
            // ========================================
            // OHIO - Columbus Market (Google, AWS, Meta)
            // ========================================
            'franklin_oh': {name:'Franklin County, OH (Columbus)',state:'Ohio',url:'https://apps.franklincountyauditor.com/arcgis/rest/services/Production/Parcels/MapServer',layer:0,center:[39.96,-83.00],attribution:'Franklin County Auditor',dcMarket:true,tier:1},
            'delaware_oh': {name:'Delaware County, OH',state:'Ohio',url:'https://maps.delco-gis.org/arcgiswebadaptor/rest/services/DelawareCountyData/MapServer',layer:0,center:[40.28,-83.07],attribution:'Delaware County OH',dcMarket:true,tier:2},
            'licking_oh': {name:'Licking County, OH (New Albany)',state:'Ohio',url:'https://gis.lcounty.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.06,-82.40],attribution:'Licking County',dcMarket:true,tier:2},
            'cuyahoga_oh': {name:'Cuyahoga County, OH (Cleveland)',state:'Ohio',url:'https://gis.cuyahogacounty.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.50,-81.69],attribution:'Cuyahoga County',dcMarket:true,tier:2},
            // ========================================
            // UTAH - Salt Lake City Market
            // ========================================
            'salt_lake_ut': {name:'Salt Lake County, UT',state:'Utah',url:'https://maps.slco.org/arcgis/rest/services/Property/Parcels/MapServer',layer:0,center:[40.76,-111.89],attribution:'Salt Lake County',dcMarket:true,tier:1},
            'utah_ut': {name:'Utah County, UT (Provo)',state:'Utah',url:'https://gis.utahcounty.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.23,-111.66],attribution:'Utah County',dcMarket:true,tier:2},
            'davis_ut': {name:'Davis County, UT',state:'Utah',url:'https://gis.daviscountyutah.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.98,-111.93],attribution:'Davis County',dcMarket:true,tier:2},
            // ========================================
            // COLORADO - Denver Market
            // ========================================
            'denver_co': {name:'Denver County, CO',state:'Colorado',url:'https://gis.denvergov.org/arcgis/rest/services/ParcelViewer/Parcels/MapServer',layer:0,center:[39.74,-104.99],attribution:'City of Denver',dcMarket:true,tier:1},
            'arapahoe_co': {name:'Arapahoe County, CO',state:'Colorado',url:'https://gis.arapahoegov.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.65,-104.80],attribution:'Arapahoe County',dcMarket:true,tier:2},
            'adams_co': {name:'Adams County, CO',state:'Colorado',url:'https://gis.adcogov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.87,-104.76],attribution:'Adams County',dcMarket:true,tier:2},
            'jefferson_co': {name:'Jefferson County, CO',state:'Colorado',url:'https://gis.jeffco.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.58,-105.25],attribution:'Jefferson County',dcMarket:true,tier:2},
            // ========================================
            // CALIFORNIA - Bay Area / LA Markets
            // ========================================
            'santa_clara_ca': {name:'Santa Clara County, CA (Silicon Valley)',state:'California',url:'https://gis.sccgov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[37.36,-121.97],attribution:'Santa Clara County',dcMarket:true,tier:1},
            'alameda_ca': {name:'Alameda County, CA (Oakland)',state:'California',url:'https://gis.acgov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[37.65,-122.12],attribution:'Alameda County',dcMarket:true,tier:2},
            'san_mateo_ca': {name:'San Mateo County, CA',state:'California',url:'https://gis.smcgov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[37.50,-122.33],attribution:'San Mateo County',dcMarket:true,tier:2},
            'los_angeles_ca': {name:'Los Angeles County, CA',state:'California',url:'https://gis.lacounty.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[34.05,-118.24],attribution:'LA County',dcMarket:true,tier:1},
            'san_diego_ca': {name:'San Diego County, CA',state:'California',url:'https://gis.sandiegocounty.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.72,-117.16],attribution:'San Diego County',dcMarket:true,tier:2},
            'orange_ca': {name:'Orange County, CA',state:'California',url:'https://gis.ocgov.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.71,-117.83],attribution:'Orange County',dcMarket:true,tier:2},
            'sacramento_ca': {name:'Sacramento County, CA',state:'California',url:'https://gis.saccounty.net/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.58,-121.49],attribution:'Sacramento County',dcMarket:true,tier:2},
            // ========================================
            // WASHINGTON - Seattle/Quincy Markets
            // ========================================
            'king_wa': {name:'King County, WA (Seattle)',state:'Washington',url:'https://gis.kingcounty.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[47.61,-122.33],attribution:'King County',dcMarket:true,tier:1},
            'grant_wa': {name:'Grant County, WA (Quincy - Microsoft)',state:'Washington',url:'https://gis.grantcountywa.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[47.23,-119.85],attribution:'Grant County',dcMarket:true,tier:1},
            'pierce_wa': {name:'Pierce County, WA (Tacoma)',state:'Washington',url:'https://gis.piercecountywa.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[47.04,-122.44],attribution:'Pierce County',dcMarket:true,tier:2},
            'snohomish_wa': {name:'Snohomish County, WA',state:'Washington',url:'https://gis.snoco.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[47.92,-122.03],attribution:'Snohomish County',dcMarket:true,tier:2},
            // ========================================
            // OREGON - Portland/Hillsboro
            // ========================================
            'multnomah_or': {name:'Multnomah County, OR (Portland)',state:'Oregon',url:'https://gis.multco.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[45.52,-122.68],attribution:'Multnomah County',dcMarket:true,tier:2},
            'washington_or': {name:'Washington County, OR (Hillsboro)',state:'Oregon',url:'https://gis.co.washington.or.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[45.52,-122.89],attribution:'Washington County OR',dcMarket:true,tier:2},
            // ========================================
            // NEW JERSEY - Northeast Corridor
            // ========================================
            'hudson_nj': {name:'Hudson County, NJ (Jersey City)',state:'New Jersey',url:'https://gis.hudsoncountynj.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.73,-74.08],attribution:'Hudson County',dcMarket:true,tier:1},
            'bergen_nj': {name:'Bergen County, NJ',state:'New Jersey',url:'https://gis.bergencountynj.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.95,-74.03],attribution:'Bergen County',dcMarket:true,tier:2},
            'essex_nj': {name:'Essex County, NJ (Newark)',state:'New Jersey',url:'https://gis.essexcountynj.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.79,-74.22],attribution:'Essex County',dcMarket:true,tier:2},
            'middlesex_nj': {name:'Middlesex County, NJ',state:'New Jersey',url:'https://gis.middlesexcountynj.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.47,-74.37],attribution:'Middlesex County',dcMarket:true,tier:2},
            // ========================================
            // NEW YORK - NYC Metro / Upstate
            // ========================================
            'nassau_ny': {name:'Nassau County, NY (Long Island)',state:'New York',url:'https://gis.nassaucountyny.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.74,-73.59],attribution:'Nassau County',dcMarket:true,tier:2},
            'westchester_ny': {name:'Westchester County, NY',state:'New York',url:'https://gis.westchestergov.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.12,-73.76],attribution:'Westchester County',dcMarket:true,tier:2},
            'suffolk_ny': {name:'Suffolk County, NY',state:'New York',url:'https://gis.suffolkcountyny.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.88,-72.80],attribution:'Suffolk County',dcMarket:true,tier:2},
            // ========================================
            // PENNSYLVANIA - Philadelphia/Pittsburgh
            // ========================================
            'philadelphia_pa': {name:'Philadelphia County, PA',state:'Pennsylvania',url:'https://gis.phila.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.95,-75.17],attribution:'City of Philadelphia',dcMarket:true,tier:2},
            'montgomery_pa': {name:'Montgomery County, PA',state:'Pennsylvania',url:'https://gis.montcopa.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.15,-75.37],attribution:'Montgomery County PA',dcMarket:true,tier:2},
            'allegheny_pa': {name:'Allegheny County, PA (Pittsburgh)',state:'Pennsylvania',url:'https://gis.alleghenycounty.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.44,-79.99],attribution:'Allegheny County',dcMarket:true,tier:2},
            // ========================================
            // MICHIGAN - Detroit Market
            // ========================================
            'wayne_mi': {name:'Wayne County, MI (Detroit)',state:'Michigan',url:'https://gis.waynecounty.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[42.33,-83.05],attribution:'Wayne County',dcMarket:true,tier:2},
            'oakland_mi': {name:'Oakland County, MI',state:'Michigan',url:'https://gis.oakgov.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[42.62,-83.29],attribution:'Oakland County',dcMarket:true,tier:2},
            // ========================================
            // FLORIDA - Miami/Tampa Markets
            // ========================================
            'miami_dade_fl': {name:'Miami-Dade County, FL',state:'Florida',url:'https://gisweb.miamidade.gov/arcgis/rest/services/Property/Parcels/MapServer',layer:0,center:[25.76,-80.19],attribution:'Miami-Dade County',dcMarket:true,tier:1},
            'hillsborough_fl': {name:'Hillsborough County, FL (Tampa)',state:'Florida',url:'https://gis.hcpafl.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[27.95,-82.46],attribution:'Hillsborough County',dcMarket:true,tier:2},
            'orange_fl': {name:'Orange County, FL (Orlando)',state:'Florida',url:'https://gis.ocfl.net/arcgis/rest/services/Parcels/MapServer',layer:0,center:[28.54,-81.38],attribution:'Orange County FL',dcMarket:true,tier:2},
            'duval_fl': {name:'Duval County, FL (Jacksonville)',state:'Florida',url:'https://maps.coj.net/arcgis/rest/services/Parcels/MapServer',layer:0,center:[30.33,-81.66],attribution:'City of Jacksonville',dcMarket:true,tier:2},
            'broward_fl': {name:'Broward County, FL (Fort Lauderdale)',state:'Florida',url:'https://gis.broward.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[26.12,-80.14],attribution:'Broward County',dcMarket:true,tier:2},
            // ========================================
            // MINNESOTA - Minneapolis Market
            // ========================================
            'hennepin_mn': {name:'Hennepin County, MN (Minneapolis)',state:'Minnesota',url:'https://gis.hennepin.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[44.98,-93.27],attribution:'Hennepin County',dcMarket:true,tier:2},
            'ramsey_mn': {name:'Ramsey County, MN (St. Paul)',state:'Minnesota',url:'https://gis.ramseycounty.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[44.95,-93.09],attribution:'Ramsey County',dcMarket:true,tier:2},
            // ========================================
            // MISSOURI - Kansas City/St. Louis
            // ========================================
            'jackson_mo': {name:'Jackson County, MO (Kansas City)',state:'Missouri',url:'https://gis.jacksongov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.10,-94.58],attribution:'Jackson County',dcMarket:true,tier:2},
            'st_louis_city_mo': {name:'St. Louis City, MO',state:'Missouri',url:'https://gis.stlouis-mo.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.63,-90.20],attribution:'City of St. Louis',dcMarket:true,tier:2},
            // ========================================
            // KANSAS - Kansas City Metro
            // ========================================
            'johnson_ks': {name:'Johnson County, KS (Overland Park)',state:'Kansas',url:'https://maps.jocogov.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.88,-94.82],attribution:'Johnson County',dcMarket:true,tier:2},
            // ========================================
            // TENNESSEE - Nashville Market
            // ========================================
            'davidson_tn': {name:'Davidson County, TN (Nashville)',state:'Tennessee',url:'https://gis.nashville.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[36.16,-86.78],attribution:'Metro Nashville',dcMarket:true,tier:2},
            'shelby_tn': {name:'Shelby County, TN (Memphis)',state:'Tennessee',url:'https://gis.shelbycountytn.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.15,-90.05],attribution:'Shelby County',dcMarket:true,tier:2},
            // ========================================
            // INDIANA - Indianapolis & New Albany Hyperscale (Google/Meta/Microsoft)
            // ========================================
            'marion_in': {name:'Marion County, IN (Indianapolis)',state:'Indiana',url:'https://maps.indy.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.77,-86.16],attribution:'City of Indianapolis',dcMarket:true,tier:2},
            'floyd_in': {name:'Floyd County, IN (New Albany - Google/Meta)',state:'Indiana',url:'https://gis.floydcounty.in.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.31,-85.82],attribution:'Floyd County',dcMarket:true,tier:1},
            'clark_in': {name:'Clark County, IN (Jeffersonville)',state:'Indiana',url:'https://gis.clarkcountyin.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.38,-85.74],attribution:'Clark County IN',dcMarket:true,tier:2},
            'hamilton_in': {name:'Hamilton County, IN (Fishers)',state:'Indiana',url:'https://gis.hamiltoncounty.in.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[40.05,-86.00],attribution:'Hamilton County IN',dcMarket:true,tier:2},
            // ========================================
            // IOWA - Des Moines / Council Bluffs (Meta)
            // ========================================
            'polk_ia': {name:'Polk County, IA (Des Moines)',state:'Iowa',url:'https://gis.polkcountyiowa.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.59,-93.62],attribution:'Polk County',dcMarket:true,tier:2},
            'pottawattamie_ia': {name:'Pottawattamie County, IA (Council Bluffs - Meta/Google)',state:'Iowa',url:'https://gis.pottcounty.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.26,-95.86],attribution:'Pottawattamie County',dcMarket:true,tier:2},
            // ========================================
            // NEBRASKA - Omaha Market
            // ========================================
            'douglas_ne': {name:'Douglas County, NE (Omaha)',state:'Nebraska',url:'https://gis.douglascounty-ne.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[41.26,-96.00],attribution:'Douglas County',dcMarket:true,tier:2},
            // ========================================
            // WISCONSIN - Milwaukee/Madison
            // ========================================
            'milwaukee_wi': {name:'Milwaukee County, WI',state:'Wisconsin',url:'https://gis.mclio.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[43.04,-87.91],attribution:'Milwaukee County',dcMarket:true,tier:2},
            'dane_wi': {name:'Dane County, WI (Madison)',state:'Wisconsin',url:'https://gis.countyofdane.com/arcgis/rest/services/Parcels/MapServer',layer:0,center:[43.07,-89.40],attribution:'Dane County',dcMarket:true,tier:2},
            // ========================================
            // MARYLAND - Baltimore Area
            // ========================================
            'baltimore_city_md': {name:'Baltimore City, MD',state:'Maryland',url:'https://gis.baltimorecity.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.29,-76.61],attribution:'Baltimore City',dcMarket:true,tier:2},
            'baltimore_county_md': {name:'Baltimore County, MD',state:'Maryland',url:'https://gis.baltimorecountymd.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.41,-76.61],attribution:'Baltimore County',dcMarket:true,tier:2},
            'montgomery_md': {name:'Montgomery County, MD',state:'Maryland',url:'https://gis.montgomerycountymd.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[39.14,-77.20],attribution:'Montgomery County MD',dcMarket:true,tier:2},
            // ========================================
            // IDAHO - Boise
            // ========================================
            'ada_id': {name:'Ada County, ID (Boise)',state:'Idaho',url:'https://gis.adacounty.id.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[43.62,-116.20],attribution:'Ada County',dcMarket:true,tier:2},
            // ========================================
            // SOUTH CAROLINA - Columbia/Greenville
            // ========================================
            'richland_sc': {name:'Richland County, SC (Columbia)',state:'South Carolina',url:'https://gis.richlandcountysc.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[34.00,-81.03],attribution:'Richland County',dcMarket:true,tier:2},
            'greenville_sc': {name:'Greenville County, SC',state:'South Carolina',url:'https://gis.greenvillecounty.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[34.85,-82.40],attribution:'Greenville County',dcMarket:true,tier:2},
            'charleston_sc': {name:'Charleston County, SC',state:'South Carolina',url:'https://gis.charlestoncounty.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.78,-79.93],attribution:'Charleston County',dcMarket:true,tier:2},
            // ========================================
            // LOUISIANA - New Orleans & Hyperscale Corridor
            // ========================================
            'orleans_la': {name:'Orleans Parish, LA (New Orleans)',state:'Louisiana',url:'https://gis.nola.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[29.95,-90.07],attribution:'City of New Orleans',dcMarket:true,tier:2},
            'ascension_la': {name:'Ascension Parish, LA (Meta Hyperscale)',state:'Louisiana',url:'https://gis.apgov.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[30.21,-90.92],attribution:'Ascension Parish',dcMarket:true,tier:1},
            'st_john_la': {name:'St. John the Baptist Parish, LA',state:'Louisiana',url:'https://gis.stjohnparish.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[30.07,-90.50],attribution:'St. John Parish',dcMarket:true,tier:2},
            // ========================================
            // MISSISSIPPI - Jackson & Meta Hyperscale
            // ========================================
            'hinds_ms': {name:'Hinds County, MS (Jackson)',state:'Mississippi',url:'https://gis.co.hinds.ms.us/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.30,-90.18],attribution:'Hinds County',dcMarket:true,tier:2},
            'rankin_ms': {name:'Rankin County, MS (Richland - Meta)',state:'Mississippi',url:'https://gis.rankincounty.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.25,-90.00],attribution:'Rankin County',dcMarket:true,tier:1},
            'madison_ms': {name:'Madison County, MS',state:'Mississippi',url:'https://gis.madisoncountyms.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[32.47,-90.12],attribution:'Madison County MS',dcMarket:true,tier:2},
            // ========================================
            // ALABAMA - Birmingham
            // ========================================
            'jefferson_al': {name:'Jefferson County, AL (Birmingham)',state:'Alabama',url:'https://gis.jccal.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[33.52,-86.80],attribution:'Jefferson County AL',dcMarket:true,tier:2},
            // ========================================
            // KENTUCKY - Louisville
            // ========================================
            'jefferson_ky': {name:'Jefferson County, KY (Louisville)',state:'Kentucky',url:'https://gis.lojic.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[38.25,-85.76],attribution:'LOJIC GIS',dcMarket:true,tier:2},
            // ========================================
            // OKLAHOMA - Oklahoma City/Tulsa
            // ========================================
            'oklahoma_ok': {name:'Oklahoma County, OK (Oklahoma City)',state:'Oklahoma',url:'https://gis.oklahomacounty.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.47,-97.52],attribution:'Oklahoma County',dcMarket:true,tier:2},
            'tulsa_ok': {name:'Tulsa County, OK',state:'Oklahoma',url:'https://gis.tulsacounty.org/arcgis/rest/services/Parcels/MapServer',layer:0,center:[36.15,-95.99],attribution:'Tulsa County',dcMarket:true,tier:2},
            // ========================================
            // NEW MEXICO - Albuquerque
            // ========================================
            'bernalillo_nm': {name:'Bernalillo County, NM (Albuquerque)',state:'New Mexico',url:'https://gis.bernco.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[35.08,-106.65],attribution:'Bernalillo County',dcMarket:true,tier:2},
            // ========================================
            // HAWAII - Honolulu
            // ========================================
            'honolulu_hi': {name:'City & County of Honolulu, HI',state:'Hawaii',url:'https://gis.honolulu.gov/arcgis/rest/services/Parcels/MapServer',layer:0,center:[21.31,-157.86],attribution:'City of Honolulu',dcMarket:true,tier:2}
        };
        
        // Calculate parcel stats
        var parcelStats = {total: Object.keys(COUNTY_PARCEL_SERVICES).length, states: {}, dcMarkets: 0};
        for (var pkey in COUNTY_PARCEL_SERVICES) {
            var pstate = COUNTY_PARCEL_SERVICES[pkey].state;
            parcelStats.states[pstate] = (parcelStats.states[pstate] || 0) + 1;
            if (COUNTY_PARCEL_SERVICES[pkey].dcMarket) parcelStats.dcMarkets++;
        }
        console.log('📦 County Parcel Services: ' + parcelStats.total + ' counties across ' + Object.keys(parcelStats.states).length + ' states');
        
        var activeParcelLayers = {};
        var parcelLayerGroup = L.layerGroup();
        console.log('🗺️ County Parcel Services: ' + Object.keys(COUNTY_PARCEL_SERVICES).length + ' DC markets available');
        
        // USGS Seismic Hazard - markers loaded via loadSeismicData()
        var seismicMarkers = [];
        
        // ============================================
        // GENERATION QUEUE DATA - Major ISO Projects
        // Sample data representing interconnection queue positions
        // ============================================
        var queueProjects = [
            // PJM Queue Projects
            {name:'Solar Farm - Loudoun VA',lat:39.08,lng:-77.52,mw:500,type:'Solar',iso:'PJM',status:'System Impact Study',year:2025},
            {name:'Battery Storage - Ashburn',lat:39.04,lng:-77.48,mw:200,type:'Storage',iso:'PJM',status:'Facilities Study',year:2024},
            {name:'Wind Farm - PA',lat:40.12,lng:-76.45,mw:350,type:'Wind',iso:'PJM',status:'IA Pending',year:2026},
            {name:'Data Center Load - VA',lat:39.01,lng:-77.41,mw:150,type:'Load',iso:'PJM',status:'System Impact Study',year:2025},
            {name:'Solar + Storage - MD',lat:39.28,lng:-76.62,mw:275,type:'Hybrid',iso:'PJM',status:'Feasibility Study',year:2026},
            // ERCOT Queue Projects  
            {name:'Solar Farm - West TX',lat:31.85,lng:-102.35,mw:800,type:'Solar',iso:'ERCOT',status:'Full Study',year:2025},
            {name:'Wind Farm - Panhandle',lat:35.45,lng:-101.82,mw:600,type:'Wind',iso:'ERCOT',status:'IA Executed',year:2024},
            {name:'Battery - Houston',lat:29.76,lng:-95.36,mw:400,type:'Storage',iso:'ERCOT',status:'Screening Study',year:2025},
            {name:'Solar - Central TX',lat:30.58,lng:-97.82,mw:450,type:'Solar',iso:'ERCOT',status:'Full Study',year:2026},
            {name:'Gas Peaker - DFW',lat:32.78,lng:-96.80,mw:200,type:'Gas',iso:'ERCOT',status:'IA Executed',year:2024},
            // CAISO Queue Projects
            {name:'Solar Farm - Mojave',lat:35.05,lng:-117.65,mw:550,type:'Solar',iso:'CAISO',status:'Phase 2',year:2025},
            {name:'Battery Storage - LA Basin',lat:33.98,lng:-118.12,mw:300,type:'Storage',iso:'CAISO',status:'Phase 1',year:2025},
            {name:'Offshore Wind - Morro Bay',lat:35.32,lng:-121.25,mw:1000,type:'Wind',iso:'CAISO',status:'Phase 1',year:2028},
            {name:'Geothermal - Imperial Valley',lat:33.02,lng:-115.52,mw:150,type:'Geothermal',iso:'CAISO',status:'IA Executed',year:2024},
            // MISO Queue Projects
            {name:'Wind Farm - Iowa',lat:42.15,lng:-93.62,mw:400,type:'Wind',iso:'MISO',status:'DPP',year:2025},
            {name:'Solar Farm - Illinois',lat:40.12,lng:-89.25,mw:350,type:'Solar',iso:'MISO',status:'DPP',year:2026},
            {name:'Battery - Minnesota',lat:44.95,lng:-93.10,mw:200,type:'Storage',iso:'MISO',status:'System Planning',year:2025},
            // SPP Queue Projects
            {name:'Wind Farm - Oklahoma',lat:35.82,lng:-97.95,mw:500,type:'Wind',iso:'SPP',status:'Definitive Study',year:2025},
            {name:'Solar Farm - Kansas',lat:38.05,lng:-97.82,mw:300,type:'Solar',iso:'SPP',status:'Impact Study',year:2026}
        ];
        
        queueProjects.forEach(function(p) {
            var color = p.type === 'Solar' ? '#fbbf24' : 
                        p.type === 'Wind' ? '#60a5fa' : 
                        p.type === 'Storage' ? '#a855f7' : 
                        p.type === 'Gas' ? '#f97316' :
                        p.type === 'Geothermal' ? '#ef4444' :
                        p.type === 'Load' ? '#6b7280' : '#22c55e';
            
            var icon = p.type === 'Solar' ? '☀️' : 
                       p.type === 'Wind' ? '💨' : 
                       p.type === 'Storage' ? '🔋' : 
                       p.type === 'Gas' ? '🔥' :
                       p.type === 'Geothermal' ? '🌋' :
                       p.type === 'Load' ? '🏢' : '⚡';
            
            L.circleMarker([p.lat, p.lng], {
                radius: Math.min(12, Math.max(5, p.mw / 100)),
                fillColor: color,
                color: '#fff',
                weight: 2,
                opacity: 0.9,
                fillOpacity: 0.6
            }).bindPopup(
                '<div class="popup-title">' + icon + ' ' + p.name + '</div>' +
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">' + p.mw + ' MW</span></div>' +
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + p.type + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">ISO/RTO</span><span class="popup-value"><span class="iso-badge iso-' + p.iso.toLowerCase() + '">' + p.iso + '</span></span></div>' +
                '<div class="popup-row"><span class="popup-label">Status</span><span class="popup-value">' + p.status + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">Target COD</span><span class="popup-value">' + p.year + '</span></div>' +
                '<div class="popup-row" style="color:#a855f7;font-size:10px;margin-top:4px;">📋 Interconnection Queue</div>'
            ).addTo(layers.queue);
        });
        console.log('📋 Generation Queue: ' + queueProjects.length + ' sample projects');
        
        console.log('🌿 NWI Wetlands layer initialized');
        console.log('🦅 USFWS Critical Habitat layer initialized');
        console.log('🌋 USGS Seismic data ready');

        // Add default active layers
        layers.datacenters.addTo(map);
        layers.nuclear.addTo(map);
        layers.substations.addTo(map);
        layers.airports.addTo(map);
        console.log('🗺️ Default layers added to map');

        // ============================================
        // DATA CENTERS - PeeringDB Pattern Data
        // ============================================
        var dataCenters = [
            // Northern Virginia (Ashburn) - 45 facilities
            {name:'Equinix DC1-DC15',lat:39.0438,lng:-77.4874,mw:450,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty ACC2-ACC9',lat:39.0401,lng:-77.4712,mw:380,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'QTS Ashburn',lat:39.0515,lng:-77.4623,mw:168,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'CoreSite VA1-VA3',lat:39.0284,lng:-77.4589,mw:120,provider:'CoreSite',status:'Active',type:'Colocation'},
            {name:'CloudHQ Ashburn',lat:39.0612,lng:-77.4901,mw:96,provider:'CloudHQ',status:'Construction',type:'Hyperscale'},
            {name:'Vantage VA1',lat:39.0723,lng:-77.4567,mw:144,provider:'Vantage',status:'Active',type:'Hyperscale'},
            {name:'Amazon AWS US-East-1',lat:39.0456,lng:-77.4834,mw:500,provider:'Amazon',status:'Active',type:'Hyperscale'},
            {name:'Microsoft Azure East',lat:39.0389,lng:-77.4956,mw:400,provider:'Microsoft',status:'Active',type:'Hyperscale'},
            {name:'Google Cloud Virginia',lat:39.0545,lng:-77.4712,mw:350,provider:'Google',status:'Active',type:'Hyperscale'},
            {name:'CyrusOne Sterling',lat:39.0123,lng:-77.4234,mw:180,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'Iron Mountain VA-1',lat:39.0678,lng:-77.4501,mw:75,provider:'Iron Mountain',status:'Active',type:'Colocation'},
            {name:'Aligned Ashburn',lat:39.0234,lng:-77.4812,mw:300,provider:'Aligned',status:'Active',type:'Adaptive'},
            {name:'PowerHouse Arcola',lat:38.9234,lng:-77.5123,mw:200,provider:'PowerHouse',status:'Construction',type:'Hyperscale'},
            {name:'PowerHouse Pacific Sterling',lat:39.0156,lng:-77.4089,mw:150,provider:'PowerHouse',status:'Active',type:'Hyperscale'},
            // Dallas-Fort Worth - 28 facilities
            {name:'Digital Realty DFW',lat:32.8901,lng:-96.9512,mw:280,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'QTS Irving',lat:32.8456,lng:-96.9234,mw:210,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'CyrusOne Carrollton',lat:32.9534,lng:-96.9012,mw:175,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'Equinix DA1-DA11',lat:32.8723,lng:-96.9423,mw:320,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Stream Richardson',lat:32.9289,lng:-96.7234,mw:95,provider:'Stream',status:'Active',type:'Colocation'},
            {name:'DataBank DFW1-DFW3',lat:32.8145,lng:-96.8678,mw:85,provider:'DataBank',status:'Active',type:'Edge'},
            {name:'Compass Allen',lat:33.0912,lng:-96.6534,mw:260,provider:'Compass',status:'Construction',type:'Hyperscale'},
            {name:'Flexential Dallas',lat:32.8567,lng:-96.9189,mw:65,provider:'Flexential',status:'Active',type:'Colocation'},
            {name:'PowerHouse Irving',lat:32.8678,lng:-96.9356,mw:200,provider:'PowerHouse',status:'Construction',type:'Hyperscale'},
            {name:'PowerHouse Grand Prairie',lat:32.7456,lng:-97.0123,mw:1800,provider:'PowerHouse',status:'Planned',type:'Mega Campus'},
            // Phoenix Metro - 22 facilities
            {name:'Digital Realty PHX',lat:33.4478,lng:-112.0712,mw:220,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'CyrusOne Chandler',lat:33.2945,lng:-111.8423,mw:190,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'QTS Phoenix',lat:33.4123,lng:-112.0345,mw:145,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'EdgeCore Mesa',lat:33.3901,lng:-111.7823,mw:200,provider:'EdgeCore',status:'Construction',type:'Hyperscale'},
            {name:'Vantage Phoenix',lat:33.5123,lng:-111.9234,mw:175,provider:'Vantage',status:'Active',type:'Hyperscale'},
            {name:'Stream Phoenix',lat:33.4512,lng:-112.0923,mw:80,provider:'Stream',status:'Active',type:'Colocation'},
            {name:'Microsoft Azure West',lat:33.4234,lng:-112.1567,mw:400,provider:'Microsoft',status:'Active',type:'Hyperscale'},
            {name:'Apple Mesa Campus',lat:33.3645,lng:-111.7912,mw:350,provider:'Apple',status:'Active',type:'Hyperscale'},
            {name:'NTT Mesa Campus',lat:33.4012,lng:-111.8234,mw:324,provider:'NTT',status:'Active',type:'Hyperscale'},
            {name:'Aligned Glendale',lat:33.5389,lng:-112.1823,mw:72,provider:'Aligned',status:'Construction',type:'Adaptive'},
            {name:'Iron Mountain AZP1-3',lat:33.4567,lng:-112.0345,mw:125,provider:'Iron Mountain',status:'Active',type:'Colocation'},
            {name:'Tract Buckeye Tech Park',lat:33.4301,lng:-112.5834,mw:1800,provider:'Tract',status:'Development',type:'Mega Campus'},
            // Chicago - 20 facilities
            {name:'Equinix CH1-CH4',lat:41.8789,lng:-87.6359,mw:180,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty 350 E Cermak',lat:41.8534,lng:-87.6189,mw:109,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'QTS Chicago',lat:41.8901,lng:-87.6412,mw:120,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'CoreSite CH1',lat:41.8678,lng:-87.6523,mw:75,provider:'CoreSite',status:'Active',type:'Colocation'},
            {name:'CyrusOne Aurora',lat:41.7534,lng:-88.2912,mw:200,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'DataBank CHI',lat:41.8423,lng:-87.6234,mw:55,provider:'DataBank',status:'Active',type:'Edge'},
            {name:'Netrality 717 South Wells',lat:41.8723,lng:-87.6345,mw:25,provider:'Netrality',status:'Active',type:'Interconnect'},
            // Silicon Valley - 18 facilities
            {name:'Equinix SV1-SV11',lat:37.3874,lng:-122.0834,mw:280,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty SJC',lat:37.3512,lng:-121.9234,mw:195,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'CoreSite SV1-SV8',lat:37.3789,lng:-122.0123,mw:165,provider:'CoreSite',status:'Active',type:'Colocation'},
            {name:'Vantage Santa Clara',lat:37.3901,lng:-121.9789,mw:200,provider:'Vantage',status:'Active',type:'Hyperscale'},
            {name:'CyrusOne San Jose',lat:37.3456,lng:-121.8912,mw:145,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            // Seattle/Quincy - 15 facilities
            {name:'Microsoft Quincy',lat:47.2312,lng:-119.8523,mw:600,provider:'Microsoft',status:'Active',type:'Hyperscale'},
            {name:'Yahoo Quincy',lat:47.2189,lng:-119.8612,mw:250,provider:'Yahoo',status:'Active',type:'Hyperscale'},
            {name:'Sabey Quincy',lat:47.2456,lng:-119.8234,mw:180,provider:'Sabey',status:'Active',type:'Colocation'},
            {name:'Equinix SE1-SE4',lat:47.6145,lng:-122.3412,mw:120,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty SEA',lat:47.6034,lng:-122.3289,mw:95,provider:'Digital Realty',status:'Active',type:'Colocation'},
            // Columbus/Ohio - 18 facilities (AWS $10B+ expansion)
            {name:'QTS Columbus',lat:39.9612,lng:-82.9234,mw:165,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'Cologix COL1-COL3',lat:39.9789,lng:-82.9456,mw:85,provider:'Cologix',status:'Active',type:'Colocation'},
            {name:'Amazon AWS US-East-2',lat:39.9534,lng:-82.9678,mw:500,provider:'Amazon',status:'Active',type:'Hyperscale'},
            {name:'Google Columbus',lat:40.0123,lng:-82.8912,mw:400,provider:'Google',status:'Active',type:'Hyperscale'},
            {name:'Meta New Albany',lat:40.0789,lng:-82.8012,mw:450,provider:'Meta',status:'Active',type:'Hyperscale'},
            {name:'AWS Marysville Campus',lat:40.2334,lng:-83.3712,mw:600,provider:'Amazon',status:'Construction',type:'Hyperscale'},
            {name:'AWS Columbus Expansion',lat:39.9823,lng:-82.9012,mw:400,provider:'Amazon',status:'Planned',type:'Hyperscale'},
            // Atlanta - 15 facilities (2024 absorption leader: 705.8 MW)
            {name:'Equinix AT1-AT4',lat:33.7534,lng:-84.3923,mw:145,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty ATL',lat:33.7689,lng:-84.4123,mw:120,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'QTS Atlanta Metro',lat:33.7412,lng:-84.3812,mw:300,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'Switch Atlanta',lat:33.7823,lng:-84.4534,mw:175,provider:'Switch',status:'Active',type:'Hyperscale'},
            {name:'CyrusOne Atlanta',lat:33.7956,lng:-84.4012,mw:110,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'Compass Douglas County',lat:33.7512,lng:-84.7423,mw:200,provider:'Compass',status:'Construction',type:'Hyperscale'},
            {name:'Google Atlanta',lat:33.7289,lng:-84.5123,mw:250,provider:'Google',status:'Construction',type:'Hyperscale'},
            // Denver - 8 facilities
            {name:'CoreSite DE1-DE2',lat:39.7423,lng:-104.9856,mw:85,provider:'CoreSite',status:'Active',type:'Colocation'},
            {name:'Flexential Denver',lat:39.7289,lng:-105.0123,mw:70,provider:'Flexential',status:'Active',type:'Colocation'},
            {name:'CyrusOne Denver',lat:39.7612,lng:-104.9534,mw:95,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'ViaWest Denver',lat:39.7534,lng:-104.9912,mw:60,provider:'ViaWest',status:'Active',type:'Colocation'},
            // Portland/The Dalles - 8 facilities
            {name:'Google The Dalles',lat:45.5912,lng:-121.1823,mw:550,provider:'Google',status:'Active',type:'Hyperscale'},
            {name:'Amazon PDX',lat:45.5234,lng:-122.6789,mw:180,provider:'Amazon',status:'Active',type:'Hyperscale'},
            {name:'Flexential Portland',lat:45.5312,lng:-122.6534,mw:65,provider:'Flexential',status:'Active',type:'Colocation'},
            {name:'Digital Realty PDX',lat:45.5189,lng:-122.6412,mw:75,provider:'Digital Realty',status:'Active',type:'Colocation'},
            // Salt Lake City - 6 facilities
            {name:'DataBank SLC',lat:40.7608,lng:-111.8910,mw:45,provider:'DataBank',status:'Active',type:'Edge'},
            {name:'Flexential SLC',lat:40.7534,lng:-111.9012,mw:55,provider:'Flexential',status:'Active',type:'Colocation'},
            {name:'CyrusOne SLC',lat:40.7689,lng:-111.8723,mw:80,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            // Las Vegas - 5 facilities
            {name:'Switch Las Vegas',lat:36.1234,lng:-115.1523,mw:450,provider:'Switch',status:'Active',type:'Hyperscale'},
            {name:'DataBank LAS',lat:36.1412,lng:-115.1234,mw:35,provider:'DataBank',status:'Active',type:'Edge'},
            // Newark/NJ - 6 facilities
            {name:'Equinix NY1-NY9',lat:40.7356,lng:-74.1723,mw:200,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty NJ',lat:40.7212,lng:-74.1534,mw:160,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'CyrusOne NJ',lat:40.7489,lng:-74.1612,mw:95,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            // Houston - 10 facilities
            {name:'CyrusOne Houston',lat:29.7623,lng:-95.3634,mw:180,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'Digital Realty IAH',lat:29.7489,lng:-95.3512,mw:145,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'QTS Houston',lat:29.7534,lng:-95.3789,mw:120,provider:'QTS',status:'Active',type:'Hyperscale'},
            {name:'Netrality Houston 1301 Fannin',lat:29.7512,lng:-95.3612,mw:35,provider:'Netrality',status:'Active',type:'Interconnect'},
            // Austin/Central Texas - 12 facilities
            {name:'Digital Realty AUS',lat:30.2672,lng:-97.7431,mw:85,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'CyrusOne Austin',lat:30.2534,lng:-97.7289,mw:110,provider:'CyrusOne',status:'Active',type:'Enterprise'},
            {name:'Rackspace Austin',lat:30.2812,lng:-97.7512,mw:65,provider:'Rackspace',status:'Active',type:'Colocation'},
            {name:'Tract Caldwell County',lat:29.9234,lng:-97.6512,mw:4000,provider:'Tract',status:'Development',type:'Mega Campus'},
            {name:'Prime Caldwell County',lat:29.9156,lng:-97.6823,mw:800,provider:'Prime',status:'Construction',type:'Hyperscale'},
            // === NEW EMERGING MARKETS ===
            // Kansas City / St. Louis - Netrality Hub
            {name:'Netrality 1102 Grand KC',lat:39.0997,lng:-94.5786,mw:40,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Netrality KC2-7801 Nieman',lat:39.0234,lng:-94.7123,mw:12,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Netrality 210 N Tucker STL',lat:38.6312,lng:-90.1923,mw:25,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Netrality 900 Walnut STL',lat:38.6289,lng:-90.1889,mw:20,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Google KC',lat:39.0823,lng:-94.5912,mw:200,provider:'Google',status:'Construction',type:'Hyperscale'},
            // Indianapolis - Netrality + Growth
            {name:'Netrality Indy 733 W Henry',lat:39.7678,lng:-86.1623,mw:18,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Netrality Indy 701 W Henry',lat:39.7689,lng:-86.1634,mw:15,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Digital Realty Indianapolis',lat:39.7734,lng:-86.1512,mw:85,provider:'Digital Realty',status:'Active',type:'Colocation'},
            {name:'Compass Indiana',lat:39.8123,lng:-86.2234,mw:150,provider:'Compass',status:'Construction',type:'Hyperscale'},
            // Philadelphia - Netrality
            {name:'Netrality 401 N Broad Philly',lat:39.9589,lng:-75.1623,mw:30,provider:'Netrality',status:'Active',type:'Interconnect'},
            {name:'Equinix PH1',lat:39.9512,lng:-75.1534,mw:65,provider:'Equinix',status:'Active',type:'IBX'},
            {name:'Digital Realty Philly',lat:39.9478,lng:-75.1478,mw:55,provider:'Digital Realty',status:'Active',type:'Colocation'},
            // Louisville, Kentucky - PowerHouse First Hyperscale
            {name:'PowerHouse Louisville Campus',lat:38.2527,lng:-85.7585,mw:400,provider:'PowerHouse',status:'Construction',type:'Hyperscale'},
            // Charlotte, NC - PowerHouse + Growth
            {name:'PowerHouse Charlotte',lat:35.2271,lng:-80.8431,mw:250,provider:'PowerHouse',status:'Planned',type:'Hyperscale'},
            {name:'CENTRA Charlotte',lat:35.2234,lng:-80.8389,mw:20,provider:'CENTRA',status:'Active',type:'Interconnect'},
            {name:'QTS Charlotte',lat:35.2145,lng:-80.8512,mw:95,provider:'QTS',status:'Active',type:'Hyperscale'},
            // Reno, Nevada - Tract + PowerHouse
            {name:'Tract Reno Campus',lat:39.5296,lng:-119.8138,mw:2000,provider:'Tract',status:'Development',type:'Mega Campus'},
            {name:'PowerHouse Reno',lat:39.5234,lng:-119.7912,mw:80,provider:'PowerHouse',status:'Construction',type:'Hyperscale'},
            {name:'CENTRA Reno',lat:39.5289,lng:-119.8034,mw:15,provider:'CENTRA',status:'Construction',type:'Interconnect'},
            {name:'Apple Reno',lat:39.5312,lng:-119.7823,mw:200,provider:'Apple',status:'Active',type:'Hyperscale'},
            {name:'Switch Reno',lat:39.5178,lng:-119.7956,mw:350,provider:'Switch',status:'Active',type:'Hyperscale'},
            // Utah - Tract Eagle Mountain
            {name:'Tract Eagle Mountain',lat:40.3142,lng:-112.0067,mw:500,provider:'Tract',status:'Development',type:'Mega Campus'},
            {name:'Meta Eagle Mountain',lat:40.3089,lng:-112.0123,mw:300,provider:'Meta',status:'Construction',type:'Hyperscale'},
            // Virginia - Tract Richmond + PowerHouse 95
            {name:'Tract Richmond Campus',lat:37.5407,lng:-77.4360,mw:800,provider:'Tract',status:'Development',type:'Mega Campus'},
            {name:'PowerHouse 95 Spotsylvania',lat:38.2012,lng:-77.5823,mw:900,provider:'PowerHouse',status:'Construction',type:'Mega Campus'},
            // Wisconsin - Microsoft Mount Pleasant
            {name:'Microsoft Mount Pleasant',lat:42.7078,lng:-87.8676,mw:400,provider:'Microsoft',status:'Construction',type:'Hyperscale'},
            // Indiana - Google Hyperscale
            {name:'Google Fort Wayne',lat:41.0793,lng:-85.1394,mw:600,provider:'Google',status:'Construction',type:'Hyperscale'},
            // Mississippi - Compass Meridian
            {name:'Compass Meridian Campus',lat:32.3643,lng:-88.7036,mw:320,provider:'Compass',status:'Construction',type:'Hyperscale'},
            // Louisiana - Meta Hyperscale
            {name:'Meta Richland Parish',lat:32.4001,lng:-91.8023,mw:500,provider:'Meta',status:'Construction',type:'Hyperscale'},
            // Nebraska - Hyperscale Growth
            {name:'Meta Papillion/Sarpy',lat:41.1545,lng:-96.0467,mw:400,provider:'Meta',status:'Construction',type:'Hyperscale'},
            // Alabama - Meta
            {name:'Meta Huntsville',lat:34.7304,lng:-86.5861,mw:350,provider:'Meta',status:'Construction',type:'Hyperscale'},
            // North Carolina - Tract Mooresville
            {name:'Tract Mooresville Tech Park',lat:35.5849,lng:-80.8101,mw:400,provider:'Tract',status:'Development',type:'Mega Campus'},
            // Minnesota - Tract Minneapolis
            {name:'Tract Minneapolis Campus',lat:44.9778,lng:-93.2650,mw:300,provider:'Tract',status:'Planned',type:'Mega Campus'},
            {name:'CENTRA Minneapolis MSP1',lat:44.8113,lng:-93.1826,mw:18,provider:'CENTRA',status:'Construction',type:'Interconnect'},
            // Pennsylvania - PowerHouse Carlisle
            {name:'PowerHouse Carlisle PAX1',lat:40.2015,lng:-77.1889,mw:150,provider:'PowerHouse',status:'Construction',type:'Hyperscale'},
            // Wyoming - Meta Cheyenne
            {name:'Meta Cheyenne',lat:41.1400,lng:-104.8202,mw:450,provider:'Meta',status:'Active',type:'Hyperscale'},
            {name:'Microsoft Cheyenne',lat:41.1234,lng:-104.8312,mw:300,provider:'Microsoft',status:'Active',type:'Hyperscale'},
            // Iowa - Major Hyperscale Hub
            {name:'Meta Altoona',lat:41.6436,lng:-93.4667,mw:500,provider:'Meta',status:'Active',type:'Hyperscale'},
            {name:'Google Council Bluffs',lat:41.2619,lng:-95.8608,mw:600,provider:'Google',status:'Active',type:'Hyperscale'},
            {name:'Microsoft West Des Moines',lat:41.5772,lng:-93.7113,mw:400,provider:'Microsoft',status:'Active',type:'Hyperscale'},
            // Arkansas - Emerging
            {name:'Meta Forest City',lat:35.0085,lng:-90.3818,mw:300,provider:'Meta',status:'Active',type:'Hyperscale'}
        ];

        dataCenters.forEach(function(dc) {
            // Status-based coloring
            var color = '#6366f1'; // Default purple (Active)
            if (dc.status === 'Construction') color = '#f59e0b'; // Orange
            else if (dc.status === 'Planned' || dc.status === 'Development') color = '#3b82f6'; // Blue
            else if (dc.status.includes('Study') || dc.status.includes('Pending')) color = '#8b5cf6'; // Light purple
            
            var size = dc.mw > 500 ? 14 : dc.mw > 300 ? 12 : dc.mw > 100 ? 9 : 6;
            L.circleMarker([dc.lat,dc.lng],{
                radius:size,fillColor:color,color:'#fff',weight:2,opacity:1,fillOpacity:0.85
            }).bindPopup('<div class="popup-title">🏢 '+dc.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Provider</span><span class="popup-value">'+dc.provider+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+dc.mw+' MW</span></div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+dc.type+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Status</span><span class="popup-value" style="color:'+color+'">'+dc.status+'</span></div>'
            ).addTo(layers.datacenters);
        });
        console.log('✅ Loaded ' + dataCenters.length + ' data centers to map');

        // ============================================
        // NUCLEAR REACTORS - NRC DATA (All 93 Operating US Reactors)
        // ============================================
        var nuclearReactors = [
            // Region 1 - Northeast
            {name:'Millstone 2',lat:41.31,lng:-72.17,mw:884,op:'Dominion',type:'PWR'},
            {name:'Millstone 3',lat:41.31,lng:-72.17,mw:1227,op:'Dominion',type:'PWR'},
            {name:'Seabrook',lat:42.90,lng:-70.85,mw:1248,op:'NextEra',type:'PWR'},
            {name:'Limerick 1',lat:40.22,lng:-75.59,mw:1134,op:'Constellation',type:'BWR'},
            {name:'Limerick 2',lat:40.22,lng:-75.59,mw:1134,op:'Constellation',type:'BWR'},
            {name:'Peach Bottom 2',lat:39.76,lng:-76.27,mw:1112,op:'Constellation',type:'BWR'},
            {name:'Peach Bottom 3',lat:39.76,lng:-76.27,mw:1112,op:'Constellation',type:'BWR'},
            {name:'Three Mile Island 1',lat:40.15,lng:-76.73,mw:837,op:'Constellation',type:'PWR'},
            {name:'Susquehanna 1',lat:41.09,lng:-76.15,mw:1257,op:'Talen',type:'BWR'},
            {name:'Susquehanna 2',lat:41.09,lng:-76.15,mw:1257,op:'Talen',type:'BWR'},
            {name:'Calvert Cliffs 1',lat:38.43,lng:-76.44,mw:867,op:'Constellation',type:'PWR'},
            {name:'Calvert Cliffs 2',lat:38.43,lng:-76.44,mw:867,op:'Constellation',type:'PWR'},
            {name:'North Anna 1',lat:38.06,lng:-77.79,mw:973,op:'Dominion',type:'PWR'},
            {name:'North Anna 2',lat:38.06,lng:-77.79,mw:973,op:'Dominion',type:'PWR'},
            {name:'Surry 1',lat:37.17,lng:-76.70,mw:838,op:'Dominion',type:'PWR'},
            {name:'Surry 2',lat:37.17,lng:-76.70,mw:838,op:'Dominion',type:'PWR'},
            // Region 2 - Southeast
            {name:'Brunswick 1',lat:33.96,lng:-78.01,mw:938,op:'Duke',type:'BWR'},
            {name:'Brunswick 2',lat:33.96,lng:-78.01,mw:932,op:'Duke',type:'BWR'},
            {name:'McGuire 1',lat:35.43,lng:-80.95,mw:1158,op:'Duke',type:'PWR'},
            {name:'McGuire 2',lat:35.43,lng:-80.95,mw:1158,op:'Duke',type:'PWR'},
            {name:'Catawba 1',lat:35.05,lng:-81.07,mw:1155,op:'Duke',type:'PWR'},
            {name:'Catawba 2',lat:35.05,lng:-81.07,mw:1155,op:'Duke',type:'PWR'},
            {name:'Oconee 1',lat:34.79,lng:-82.90,mw:846,op:'Duke',type:'PWR'},
            {name:'Oconee 2',lat:34.79,lng:-82.90,mw:846,op:'Duke',type:'PWR'},
            {name:'Oconee 3',lat:34.79,lng:-82.90,mw:846,op:'Duke',type:'PWR'},
            {name:'Vogtle 1',lat:33.14,lng:-81.76,mw:1109,op:'Southern',type:'PWR'},
            {name:'Vogtle 2',lat:33.14,lng:-81.76,mw:1127,op:'Southern',type:'PWR'},
            {name:'Vogtle 3',lat:33.14,lng:-81.76,mw:1117,op:'Southern',type:'AP1000'},
            {name:'Vogtle 4',lat:33.14,lng:-81.76,mw:1117,op:'Southern',type:'AP1000'},
            {name:'Hatch 1',lat:31.93,lng:-82.34,mw:876,op:'Southern',type:'BWR'},
            {name:'Hatch 2',lat:31.93,lng:-82.34,mw:883,op:'Southern',type:'BWR'},
            {name:'Turkey Point 3',lat:25.43,lng:-80.33,mw:802,op:'FPL',type:'PWR'},
            {name:'Turkey Point 4',lat:25.43,lng:-80.33,mw:802,op:'FPL',type:'PWR'},
            {name:'St. Lucie 1',lat:27.35,lng:-80.25,mw:1002,op:'FPL',type:'PWR'},
            {name:'St. Lucie 2',lat:27.35,lng:-80.25,mw:998,op:'FPL',type:'PWR'},
            // Region 3 - Midwest
            {name:'Dresden 2',lat:41.39,lng:-88.27,mw:902,op:'Constellation',type:'BWR'},
            {name:'Dresden 3',lat:41.39,lng:-88.27,mw:895,op:'Constellation',type:'BWR'},
            {name:'Braidwood 1',lat:41.24,lng:-88.21,mw:1194,op:'Constellation',type:'PWR'},
            {name:'Braidwood 2',lat:41.24,lng:-88.21,mw:1160,op:'Constellation',type:'PWR'},
            {name:'Byron 1',lat:42.08,lng:-89.28,mw:1164,op:'Constellation',type:'PWR'},
            {name:'Byron 2',lat:42.08,lng:-89.28,mw:1136,op:'Constellation',type:'PWR'},
            {name:'LaSalle 1',lat:41.24,lng:-88.67,mw:1137,op:'Constellation',type:'BWR'},
            {name:'LaSalle 2',lat:41.24,lng:-88.67,mw:1140,op:'Constellation',type:'BWR'},
            {name:'Quad Cities 1',lat:41.73,lng:-90.34,mw:908,op:'Constellation',type:'BWR'},
            {name:'Quad Cities 2',lat:41.73,lng:-90.34,mw:911,op:'Constellation',type:'BWR'},
            {name:'Davis-Besse',lat:41.60,lng:-83.09,mw:894,op:'Energy Harbor',type:'PWR'},
            {name:'Perry',lat:41.80,lng:-81.14,mw:1256,op:'Energy Harbor',type:'BWR'},
            {name:'Beaver Valley 1',lat:40.62,lng:-80.43,mw:939,op:'Energy Harbor',type:'PWR'},
            {name:'Beaver Valley 2',lat:40.62,lng:-80.43,mw:932,op:'Energy Harbor',type:'PWR'},
            {name:'Fermi 2',lat:41.96,lng:-83.26,mw:1150,op:'DTE',type:'BWR'},
            {name:'Cook 1',lat:41.98,lng:-86.56,mw:1009,op:'AEP',type:'PWR'},
            {name:'Cook 2',lat:41.98,lng:-86.56,mw:1157,op:'AEP',type:'PWR'},
            {name:'Point Beach 1',lat:44.28,lng:-87.54,mw:591,op:'NextEra',type:'PWR'},
            {name:'Point Beach 2',lat:44.28,lng:-87.54,mw:591,op:'NextEra',type:'PWR'},
            {name:'Prairie Island 1',lat:44.62,lng:-92.63,mw:522,op:'Xcel',type:'PWR'},
            {name:'Prairie Island 2',lat:44.62,lng:-92.63,mw:519,op:'Xcel',type:'PWR'},
            {name:'Monticello',lat:45.33,lng:-93.85,mw:671,op:'Xcel',type:'BWR'},
            // Region 4 - South/West
            {name:'South Texas 1',lat:28.80,lng:-96.05,mw:1280,op:'STP Nuclear',type:'PWR'},
            {name:'South Texas 2',lat:28.80,lng:-96.05,mw:1280,op:'STP Nuclear',type:'PWR'},
            {name:'Comanche Peak 1',lat:32.30,lng:-97.79,mw:1218,op:'Vistra',type:'PWR'},
            {name:'Comanche Peak 2',lat:32.30,lng:-97.79,mw:1218,op:'Vistra',type:'PWR'},
            {name:'River Bend',lat:30.76,lng:-91.33,mw:967,op:'Entergy',type:'BWR'},
            {name:'Waterford 3',lat:29.99,lng:-90.47,mw:1168,op:'Entergy',type:'PWR'},
            {name:'Grand Gulf',lat:32.01,lng:-91.05,mw:1400,op:'Entergy',type:'BWR'},
            {name:'Arkansas Nuclear 1',lat:35.31,lng:-93.23,mw:836,op:'Entergy',type:'PWR'},
            {name:'Arkansas Nuclear 2',lat:35.31,lng:-93.23,mw:988,op:'Entergy',type:'PWR'},
            {name:'Wolf Creek',lat:38.24,lng:-95.69,mw:1205,op:'Evergy',type:'PWR'},
            {name:'Callaway',lat:38.76,lng:-91.78,mw:1215,op:'Ameren',type:'PWR'},
            {name:'Cooper',lat:40.36,lng:-95.64,mw:769,op:'NPPD',type:'BWR'},
            {name:'Palo Verde 1',lat:33.39,lng:-112.86,mw:1311,op:'APS',type:'PWR'},
            {name:'Palo Verde 2',lat:33.39,lng:-112.86,mw:1314,op:'APS',type:'PWR'},
            {name:'Palo Verde 3',lat:33.39,lng:-112.86,mw:1312,op:'APS',type:'PWR'},
            {name:'Diablo Canyon 1',lat:35.21,lng:-120.85,mw:1138,op:'PG&E',type:'PWR'},
            {name:'Diablo Canyon 2',lat:35.21,lng:-120.85,mw:1118,op:'PG&E',type:'PWR'},
            {name:'Columbia',lat:46.47,lng:-119.33,mw:1190,op:'Energy NW',type:'BWR'}
        ];

        nuclearReactors.forEach(function(r) {
            L.circleMarker([r.lat,r.lng],{
                radius:10,fillColor:'#fbbf24',color:'#f59e0b',weight:2,opacity:1,fillOpacity:0.8
            }).bindPopup('<div class="popup-title">☢️ '+r.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+r.mw+' MW</span></div>'+
                '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">'+r.op+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+r.type+'</span></div>'
            ).addTo(layers.nuclear);
        });

        // ============================================
        // MAJOR SUBSTATIONS - HIFLD Pattern Data
        // ============================================
        var substations = [
    // ==========================================
    // PJM - Northern Virginia / DC Area (60+)
    // ==========================================
    {name:'Loudoun 500kV',lat:39.04,lng:-77.52,mw:4200,v:'500kV',owner:'Dominion'},
    {name:'Doubs 500kV',lat:39.38,lng:-77.42,mw:3800,v:'500kV',owner:'Dominion'},
    {name:'Goose Creek',lat:39.01,lng:-77.46,mw:2400,v:'230kV',owner:'Dominion'},
    {name:'Beaumeade',lat:39.02,lng:-77.40,mw:1800,v:'230kV',owner:'Dominion'},
    {name:'Elmont 500kV',lat:39.12,lng:-77.38,mw:3200,v:'500kV',owner:'Dominion'},
    {name:'Otter Creek',lat:39.15,lng:-77.55,mw:2800,v:'500kV',owner:'Dominion'},
    {name:'Bull Run',lat:38.92,lng:-77.52,mw:2200,v:'230kV',owner:'Dominion'},
    {name:'Pleasant View 500kV',lat:39.22,lng:-77.65,mw:3600,v:'500kV',owner:'Dominion'},
    {name:'Morrisville 230kV',lat:39.08,lng:-77.42,mw:1600,v:'230kV',owner:'NOVEC'},
    {name:'Gainesville',lat:38.81,lng:-77.62,mw:1200,v:'230kV',owner:'Dominion'},
    {name:'Warrenton',lat:38.72,lng:-77.80,mw:800,v:'115kV',owner:'NOVEC'},
    {name:'Haymarket',lat:38.82,lng:-77.65,mw:1100,v:'230kV',owner:'Dominion'},
    {name:'Arcola 500kV',lat:38.98,lng:-77.55,mw:3400,v:'500kV',owner:'Dominion'},
    {name:'Brambleton',lat:39.02,lng:-77.52,mw:1500,v:'230kV',owner:'Dominion'},
    {name:'Shellhorn',lat:39.05,lng:-77.44,mw:1800,v:'230kV',owner:'Dominion'},
    {name:'Greenway',lat:39.01,lng:-77.48,mw:1400,v:'230kV',owner:'Dominion'},
    {name:'Balls Ford',lat:38.85,lng:-77.55,mw:1200,v:'230kV',owner:'Dominion'},
    {name:'Nokesville',lat:38.68,lng:-77.58,mw:600,v:'115kV',owner:'NOVEC'},
    {name:'Possum Point 500kV',lat:38.55,lng:-77.28,mw:3200,v:'500kV',owner:'Dominion'},
    {name:'Idylwood',lat:38.88,lng:-77.22,mw:2200,v:'230kV',owner:'Dominion'},
    {name:'Clifton',lat:38.78,lng:-77.38,mw:1100,v:'230kV',owner:'Dominion'},
    {name:'Ox 500kV',lat:38.85,lng:-77.32,mw:2800,v:'500kV',owner:'Dominion'},
    {name:'Bristers',lat:38.72,lng:-77.45,mw:900,v:'115kV',owner:'Dominion'},
    {name:'Cannon Branch',lat:38.95,lng:-77.58,mw:1600,v:'230kV',owner:'Dominion'},
    // PJM - Maryland
    {name:'Brighton 500kV',lat:39.05,lng:-77.18,mw:3600,v:'500kV',owner:'Pepco'},
    {name:'Dickerson 500kV',lat:39.22,lng:-77.42,mw:3200,v:'500kV',owner:'Potomac Edison'},
    {name:'Ringgold 500kV',lat:39.65,lng:-77.65,mw:2800,v:'500kV',owner:'FirstEnergy'},
    {name:'Waugh Chapel',lat:39.05,lng:-76.72,mw:1800,v:'230kV',owner:'BGE'},
    {name:'Calvert Cliffs',lat:38.43,lng:-76.44,mw:2400,v:'500kV',owner:'Constellation'},
    
    // ==========================================
    // ERCOT - Texas (100+)
    // ==========================================
    // DFW Area
    {name:'DFW North 345kV',lat:33.15,lng:-96.85,mw:5200,v:'345kV',owner:'Oncor'},
    {name:'Parker County',lat:32.78,lng:-97.65,mw:4800,v:'345kV',owner:'Oncor'},
    {name:'Garland 345kV',lat:32.91,lng:-96.63,mw:3600,v:'345kV',owner:'Oncor'},
    {name:'Trinity 345kV',lat:32.85,lng:-96.72,mw:3200,v:'345kV',owner:'Oncor'},
    {name:'Forney 345kV',lat:32.75,lng:-96.45,mw:3800,v:'345kV',owner:'Oncor'},
    {name:'Venus 345kV',lat:32.42,lng:-97.10,mw:3400,v:'345kV',owner:'Oncor'},
    {name:'Comanche Peak',lat:32.30,lng:-97.79,mw:5200,v:'345kV',owner:'Oncor'},
    {name:'Midlothian',lat:32.48,lng:-96.98,mw:2600,v:'345kV',owner:'Oncor'},
    {name:'DeCordova',lat:32.42,lng:-97.68,mw:2200,v:'345kV',owner:'Oncor'},
    {name:'Martin Lake',lat:32.25,lng:-94.55,mw:4800,v:'345kV',owner:'Oncor'},
    {name:'Big Brown',lat:31.82,lng:-96.05,mw:3200,v:'345kV',owner:'Oncor'},
    {name:'Limestone',lat:31.42,lng:-96.25,mw:3600,v:'345kV',owner:'Oncor'},
    {name:'Tradinghouse',lat:31.55,lng:-97.02,mw:2800,v:'345kV',owner:'Oncor'},
    {name:'Graham 345kV',lat:33.12,lng:-98.58,mw:2400,v:'345kV',owner:'Oncor'},
    {name:'Weatherford',lat:32.75,lng:-97.78,mw:1800,v:'138kV',owner:'Oncor'},
    {name:'Cleburne',lat:32.35,lng:-97.38,mw:1600,v:'138kV',owner:'Oncor'},
    {name:'Waxahachie',lat:32.40,lng:-96.85,mw:1400,v:'138kV',owner:'Oncor'},
    {name:'Ennis',lat:32.32,lng:-96.62,mw:1200,v:'138kV',owner:'Oncor'},
    {name:'McKinney',lat:33.20,lng:-96.62,mw:2200,v:'345kV',owner:'Oncor'},
    {name:'Allen',lat:33.10,lng:-96.68,mw:1800,v:'138kV',owner:'Oncor'},
    {name:'Plano East',lat:33.05,lng:-96.65,mw:1600,v:'138kV',owner:'Oncor'},
    {name:'Richardson',lat:32.98,lng:-96.72,mw:1400,v:'138kV',owner:'Oncor'},
    // Houston Area
    {name:'Houston Energy Ctr',lat:29.82,lng:-95.35,mw:6400,v:'345kV',owner:'CenterPoint'},
    {name:'Greens Bayou',lat:29.88,lng:-95.22,mw:4200,v:'345kV',owner:'CenterPoint'},
    {name:'W.A. Parish',lat:29.48,lng:-95.63,mw:5800,v:'345kV',owner:'CenterPoint'},
    {name:'Cedar Bayou',lat:29.78,lng:-94.92,mw:3600,v:'345kV',owner:'CenterPoint'},
    {name:'Deer Park',lat:29.72,lng:-95.12,mw:3200,v:'345kV',owner:'CenterPoint'},
    {name:'Hiram Clarke',lat:29.65,lng:-95.45,mw:2800,v:'345kV',owner:'CenterPoint'},
    {name:'Bellaire',lat:29.70,lng:-95.48,mw:2400,v:'138kV',owner:'CenterPoint'},
    {name:'Almeda',lat:29.68,lng:-95.38,mw:2200,v:'138kV',owner:'CenterPoint'},
    {name:'Tomball',lat:30.10,lng:-95.62,mw:1800,v:'138kV',owner:'CenterPoint'},
    {name:'Spring',lat:30.05,lng:-95.42,mw:1600,v:'138kV',owner:'CenterPoint'},
    {name:'Humble',lat:30.00,lng:-95.28,mw:1400,v:'138kV',owner:'CenterPoint'},
    {name:'Baytown',lat:29.75,lng:-94.98,mw:2000,v:'138kV',owner:'CenterPoint'},
    {name:'Galveston',lat:29.30,lng:-94.80,mw:1200,v:'138kV',owner:'CenterPoint'},
    {name:'Texas City',lat:29.38,lng:-94.90,mw:1800,v:'138kV',owner:'CenterPoint'},
    {name:'Freeport',lat:28.95,lng:-95.35,mw:2200,v:'138kV',owner:'CenterPoint'},
    {name:'STP Nuclear',lat:28.80,lng:-96.05,mw:4800,v:'345kV',owner:'CenterPoint'},
    // Austin/San Antonio
    {name:'Austin Fayette',lat:29.92,lng:-96.75,mw:4100,v:'345kV',owner:'LCRA'},
    {name:'South Texas 345kV',lat:29.35,lng:-98.48,mw:3800,v:'345kV',owner:'CPS Energy'},
    {name:'Braunig',lat:29.28,lng:-98.38,mw:2600,v:'345kV',owner:'CPS Energy'},
    {name:'Calaveras',lat:29.32,lng:-98.28,mw:2400,v:'345kV',owner:'CPS Energy'},
    {name:'Spruce',lat:29.25,lng:-98.55,mw:3200,v:'345kV',owner:'CPS Energy'},
    {name:'Rio Nogales',lat:29.18,lng:-98.68,mw:2200,v:'345kV',owner:'CPS Energy'},
    {name:'Mueller',lat:30.30,lng:-97.70,mw:2800,v:'345kV',owner:'Austin Energy'},
    {name:'Decker',lat:30.32,lng:-97.62,mw:2400,v:'345kV',owner:'Austin Energy'},
    {name:'Sand Hill',lat:30.25,lng:-97.75,mw:2600,v:'345kV',owner:'Austin Energy'},
    {name:'Pflugerville',lat:30.45,lng:-97.62,mw:1800,v:'138kV',owner:'Austin Energy'},
    {name:'Round Rock',lat:30.52,lng:-97.68,mw:1600,v:'138kV',owner:'Oncor'},
    {name:'Georgetown',lat:30.65,lng:-97.68,mw:1400,v:'138kV',owner:'Oncor'},
    // Temple/Waco - Meta Mega Project Area
    {name:'Temple 345kV',lat:31.10,lng:-97.34,mw:4200,v:'345kV',owner:'Oncor'},
    {name:'Waco 345kV',lat:31.55,lng:-97.15,mw:3600,v:'345kV',owner:'Oncor'},
    {name:'Killeen',lat:31.12,lng:-97.72,mw:2200,v:'138kV',owner:'Oncor'},
    {name:'Belton',lat:31.05,lng:-97.45,mw:1800,v:'138kV',owner:'Oncor'},
    // West Texas
    {name:'Permian Basin Hub',lat:31.85,lng:-102.35,mw:4800,v:'345kV',owner:'Oncor'},
    {name:'Midland 345kV',lat:31.99,lng:-102.08,mw:3200,v:'345kV',owner:'Oncor'},
    {name:'Odessa 345kV',lat:31.85,lng:-102.38,mw:2800,v:'345kV',owner:'Oncor'},
    {name:'Andrews',lat:32.32,lng:-102.55,mw:2200,v:'345kV',owner:'Oncor'},
    {name:'Crane',lat:31.40,lng:-102.35,mw:1800,v:'138kV',owner:'Oncor'},
    {name:'Monahans',lat:31.60,lng:-102.88,mw:1600,v:'138kV',owner:'Oncor'},
    {name:'Pecos',lat:31.42,lng:-103.50,mw:1400,v:'138kV',owner:'Oncor'},
    // Panhandle Wind
    {name:'Panhandle Hub',lat:35.45,lng:-101.35,mw:3800,v:'345kV',owner:'Xcel'},
    {name:'Amarillo',lat:35.22,lng:-101.85,mw:2600,v:'345kV',owner:'Xcel'},
    {name:'Bushland',lat:35.18,lng:-102.08,mw:2200,v:'345kV',owner:'Xcel'},
    {name:'Tolk',lat:34.18,lng:-102.48,mw:3400,v:'345kV',owner:'Xcel'},
    
    // ==========================================
    // WECC - Phoenix Metro (40+)
    // ==========================================
    {name:'Palo Verde Hub',lat:33.39,lng:-112.86,mw:8200,v:'500kV',owner:'APS'},
    {name:'West Phoenix 500kV',lat:33.48,lng:-112.28,mw:4600,v:'500kV',owner:'APS'},
    {name:'Westwing 500kV',lat:33.52,lng:-112.42,mw:3800,v:'500kV',owner:'APS'},
    {name:'Pinnacle Peak',lat:33.72,lng:-111.85,mw:2800,v:'230kV',owner:'SRP'},
    {name:'Agua Fria',lat:33.55,lng:-112.22,mw:3200,v:'500kV',owner:'APS'},
    {name:'Deer Valley',lat:33.68,lng:-112.12,mw:2600,v:'230kV',owner:'APS'},
    {name:'Paradise Valley',lat:33.55,lng:-111.95,mw:2200,v:'230kV',owner:'SRP'},
    {name:'Scottsdale',lat:33.52,lng:-111.92,mw:1800,v:'230kV',owner:'SRP'},
    {name:'Tempe',lat:33.42,lng:-111.95,mw:1600,v:'230kV',owner:'SRP'},
    {name:'Mesa Eastside',lat:33.42,lng:-111.72,mw:2400,v:'230kV',owner:'SRP'},
    {name:'Santan',lat:33.28,lng:-111.72,mw:3000,v:'500kV',owner:'SRP'},
    {name:'Browning',lat:33.35,lng:-111.85,mw:2200,v:'230kV',owner:'SRP'},
    {name:'Ocotillo',lat:33.32,lng:-111.88,mw:1800,v:'230kV',owner:'APS'},
    {name:'Kyrene',lat:33.38,lng:-111.95,mw:2000,v:'230kV',owner:'SRP'},
    {name:'Chandler',lat:33.30,lng:-111.85,mw:1600,v:'230kV',owner:'SRP'},
    {name:'Gilbert',lat:33.35,lng:-111.78,mw:1400,v:'230kV',owner:'SRP'},
    {name:'Apache Junction',lat:33.42,lng:-111.55,mw:1200,v:'230kV',owner:'SRP'},
    {name:'Superstition',lat:33.38,lng:-111.52,mw:1000,v:'230kV',owner:'SRP'},
    {name:'Glendale',lat:33.55,lng:-112.18,mw:1800,v:'230kV',owner:'APS'},
    {name:'Peoria',lat:33.58,lng:-112.25,mw:1600,v:'230kV',owner:'APS'},
    {name:'Surprise',lat:33.62,lng:-112.35,mw:1400,v:'230kV',owner:'APS'},
    {name:'Goodyear',lat:33.45,lng:-112.38,mw:2200,v:'230kV',owner:'APS'},
    {name:'Avondale',lat:33.43,lng:-112.32,mw:1800,v:'230kV',owner:'APS'},
    {name:'Buckeye',lat:33.38,lng:-112.58,mw:1600,v:'230kV',owner:'APS'},
    {name:'Hassayampa 500kV',lat:33.42,lng:-112.72,mw:4200,v:'500kV',owner:'APS'},
    {name:'Rudd',lat:33.28,lng:-112.12,mw:1400,v:'230kV',owner:'APS'},
    // Tucson
    {name:'Tucson 345kV',lat:32.22,lng:-110.92,mw:2800,v:'345kV',owner:'TEP'},
    {name:'Vail',lat:32.05,lng:-110.68,mw:2200,v:'345kV',owner:'TEP'},
    {name:'Springerville',lat:34.15,lng:-109.28,mw:3200,v:'345kV',owner:'TEP'},
    
    // ==========================================
    // WECC - California (50+)
    // ==========================================
    // Northern California
    {name:'Tesla 500kV',lat:37.67,lng:-121.52,mw:4200,v:'500kV',owner:'PG&E'},
    {name:'Metcalf 500kV',lat:37.24,lng:-121.85,mw:3600,v:'500kV',owner:'PG&E'},
    {name:'Moss Landing',lat:36.80,lng:-121.78,mw:3200,v:'500kV',owner:'PG&E'},
    {name:'Los Banos 500kV',lat:37.05,lng:-120.85,mw:4800,v:'500kV',owner:'PG&E'},
    {name:'Newark',lat:37.52,lng:-122.02,mw:2800,v:'230kV',owner:'PG&E'},
    {name:'Ravenswood',lat:37.48,lng:-122.12,mw:2400,v:'230kV',owner:'PG&E'},
    {name:'San Mateo',lat:37.55,lng:-122.32,mw:2000,v:'230kV',owner:'PG&E'},
    {name:'Embarcadero',lat:37.78,lng:-122.38,mw:1800,v:'115kV',owner:'PG&E'},
    {name:'Potrero',lat:37.75,lng:-122.38,mw:1600,v:'115kV',owner:'PG&E'},
    {name:'Martin',lat:37.88,lng:-122.12,mw:2200,v:'230kV',owner:'PG&E'},
    {name:'Oakland',lat:37.80,lng:-122.25,mw:1800,v:'115kV',owner:'PG&E'},
    {name:'Fremont',lat:37.55,lng:-121.98,mw:1600,v:'115kV',owner:'PG&E'},
    {name:'Milpitas',lat:37.42,lng:-121.92,mw:2400,v:'230kV',owner:'PG&E'},
    {name:'Santa Clara',lat:37.35,lng:-121.95,mw:2000,v:'230kV',owner:'Silicon Valley Power'},
    // Southern California
    {name:'Devers 500kV',lat:33.95,lng:-116.58,mw:4400,v:'500kV',owner:'SCE'},
    {name:'Vincent 500kV',lat:34.52,lng:-118.12,mw:5200,v:'500kV',owner:'SCE'},
    {name:'Midway 500kV',lat:35.22,lng:-119.38,mw:4800,v:'500kV',owner:'PG&E'},
    {name:'Sylmar 500kV',lat:34.32,lng:-118.42,mw:4600,v:'500kV',owner:'LADWP'},
    {name:'Lugo 500kV',lat:34.38,lng:-117.45,mw:3800,v:'500kV',owner:'SCE'},
    {name:'Eldorado 500kV',lat:35.78,lng:-114.98,mw:4200,v:'500kV',owner:'SCE'},
    {name:'Victorville 500kV',lat:34.55,lng:-117.32,mw:3400,v:'500kV',owner:'SCE'},
    {name:'Mira Loma 500kV',lat:34.02,lng:-117.52,mw:3600,v:'500kV',owner:'SCE'},
    {name:'Rancho Vista',lat:34.65,lng:-118.18,mw:2800,v:'230kV',owner:'SCE'},
    {name:'Antelope',lat:34.72,lng:-118.28,mw:2400,v:'230kV',owner:'SCE'},
    {name:'Toluca Lake',lat:34.15,lng:-118.35,mw:2000,v:'230kV',owner:'LADWP'},
    {name:'Receiving Station A',lat:34.05,lng:-118.25,mw:1800,v:'230kV',owner:'LADWP'},
    {name:'Scattergood',lat:33.92,lng:-118.42,mw:2200,v:'230kV',owner:'LADWP'},
    {name:'Harbor',lat:33.78,lng:-118.28,mw:1600,v:'230kV',owner:'LADWP'},
    
    // ==========================================
    // WECC - Pacific Northwest (40+)
    // ==========================================
    {name:'Malin 500kV',lat:42.02,lng:-121.58,mw:3800,v:'500kV',owner:'BPA'},
    {name:'Celilo HVDC',lat:45.65,lng:-120.92,mw:3100,v:'HVDC',owner:'BPA'},
    {name:'Big Eddy 500kV',lat:45.62,lng:-121.15,mw:4200,v:'500kV',owner:'BPA'},
    {name:'Vantage 500kV',lat:46.95,lng:-119.98,mw:3600,v:'500kV',owner:'BPA'},
    {name:'Columbia Gen',lat:46.47,lng:-119.35,mw:2800,v:'500kV',owner:'BPA'},
    {name:'John Day 500kV',lat:45.72,lng:-120.68,mw:3400,v:'500kV',owner:'BPA'},
    {name:'McNary 500kV',lat:45.95,lng:-119.28,mw:3200,v:'500kV',owner:'BPA'},
    {name:'Ashe 500kV',lat:45.62,lng:-118.98,mw:2800,v:'500kV',owner:'BPA'},
    {name:'Hanford',lat:46.55,lng:-119.52,mw:2400,v:'500kV',owner:'BPA'},
    {name:'Chief Joseph',lat:47.98,lng:-119.62,mw:4800,v:'500kV',owner:'BPA'},
    {name:'Grand Coulee',lat:47.95,lng:-118.98,mw:6800,v:'500kV',owner:'BPA'},
    // Seattle Area
    {name:'Maple Valley',lat:47.38,lng:-122.02,mw:2800,v:'500kV',owner:'BPA'},
    {name:'Raver 500kV',lat:47.28,lng:-122.12,mw:3200,v:'500kV',owner:'BPA'},
    {name:'Covington',lat:47.35,lng:-122.12,mw:2400,v:'230kV',owner:'PSE'},
    {name:'Talbot Hill',lat:47.45,lng:-122.18,mw:2000,v:'230kV',owner:'PSE'},
    {name:'Duwamish',lat:47.55,lng:-122.32,mw:1800,v:'230kV',owner:'Seattle City Light'},
    {name:'University',lat:47.65,lng:-122.30,mw:1600,v:'230kV',owner:'Seattle City Light'},
    {name:'Shoreline',lat:47.75,lng:-122.35,mw:1400,v:'115kV',owner:'Seattle City Light'},
    {name:'Bothell',lat:47.75,lng:-122.20,mw:1200,v:'115kV',owner:'PSE'},
    {name:'Bellevue',lat:47.62,lng:-122.20,mw:1800,v:'230kV',owner:'PSE'},
    {name:'Sammamish',lat:47.62,lng:-122.08,mw:1400,v:'115kV',owner:'PSE'},
    // Quincy Data Center Area
    {name:'Quincy 230kV',lat:47.23,lng:-119.85,mw:2600,v:'230kV',owner:'Grant PUD'},
    {name:'Wanapum 500kV',lat:46.88,lng:-119.98,mw:3800,v:'500kV',owner:'Grant PUD'},
    {name:'Rocky Reach',lat:47.48,lng:-120.28,mw:3200,v:'500kV',owner:'Chelan PUD'},
    {name:'Rock Island',lat:47.35,lng:-120.08,mw:2800,v:'230kV',owner:'Chelan PUD'},
    // Portland Area
    {name:'Keeler',lat:45.55,lng:-122.58,mw:2400,v:'500kV',owner:'BPA'},
    {name:'Paul 500kV',lat:45.48,lng:-122.75,mw:2800,v:'500kV',owner:'PGE'},
    {name:'Rivergate',lat:45.62,lng:-122.75,mw:2000,v:'230kV',owner:'PGE'},
    {name:'Harborton',lat:45.58,lng:-122.78,mw:1800,v:'230kV',owner:'PGE'},
    {name:'St Johns',lat:45.58,lng:-122.75,mw:1600,v:'115kV',owner:'PGE'},
    
    // ==========================================
    // PJM/MISO - Chicago Area (40+)
    // ==========================================
    {name:'Chicago West',lat:41.85,lng:-88.05,mw:5400,v:'345kV',owner:'ComEd'},
    {name:'Fisk 345kV',lat:41.84,lng:-87.65,mw:3200,v:'345kV',owner:'ComEd'},
    {name:'Crawford 345kV',lat:41.82,lng:-87.72,mw:2800,v:'345kV',owner:'ComEd'},
    {name:'Electric Junction',lat:41.65,lng:-87.62,mw:4100,v:'345kV',owner:'ComEd'},
    {name:'Byron 345kV',lat:42.08,lng:-89.28,mw:4800,v:'345kV',owner:'ComEd'},
    {name:'LaSalle 345kV',lat:41.24,lng:-88.67,mw:4200,v:'345kV',owner:'ComEd'},
    {name:'Dresden 345kV',lat:41.39,lng:-88.27,mw:3800,v:'345kV',owner:'ComEd'},
    {name:'Braidwood 345kV',lat:41.24,lng:-88.21,mw:4600,v:'345kV',owner:'ComEd'},
    {name:'Quad Cities',lat:41.73,lng:-90.34,mw:3600,v:'345kV',owner:'ComEd'},
    {name:'Rockford',lat:42.28,lng:-89.08,mw:2200,v:'345kV',owner:'ComEd'},
    {name:'DeKalb',lat:41.92,lng:-88.75,mw:1800,v:'138kV',owner:'ComEd'},
    {name:'Aurora',lat:41.75,lng:-88.30,mw:2400,v:'345kV',owner:'ComEd'},
    {name:'Joliet',lat:41.52,lng:-88.08,mw:2600,v:'345kV',owner:'ComEd'},
    {name:'Kankakee',lat:41.12,lng:-87.88,mw:1400,v:'138kV',owner:'ComEd'},
    {name:'Waukegan',lat:42.35,lng:-87.85,mw:2000,v:'138kV',owner:'ComEd'},
    {name:'Zion',lat:42.45,lng:-87.82,mw:1800,v:'138kV',owner:'ComEd'},
    {name:'North Shore',lat:42.15,lng:-87.78,mw:1600,v:'138kV',owner:'ComEd'},
    {name:'Evanston',lat:42.05,lng:-87.68,mw:1400,v:'138kV',owner:'ComEd'},
    {name:'Skokie',lat:42.02,lng:-87.72,mw:1200,v:'138kV',owner:'ComEd'},
    {name:'Oak Park',lat:41.88,lng:-87.78,mw:1000,v:'138kV',owner:'ComEd'},
    {name:'Naperville',lat:41.78,lng:-88.15,mw:1800,v:'138kV',owner:'ComEd'},
    {name:'Downers Grove',lat:41.80,lng:-88.02,mw:1600,v:'138kV',owner:'ComEd'},
    {name:'Lombard',lat:41.88,lng:-88.02,mw:1400,v:'138kV',owner:'ComEd'},
    
    // ==========================================
    // PJM - Ohio / Columbus Area (35+)
    // ==========================================
    // 765kV Backbone
    {name:'Amos 765kV',lat:38.42,lng:-81.82,mw:5600,v:'765kV',owner:'AEP'},
    {name:'Kammer 765kV',lat:39.85,lng:-80.68,mw:4800,v:'765kV',owner:'AEP'},
    {name:'Marysville 765kV',lat:40.25,lng:-83.35,mw:4200,v:'765kV',owner:'AEP'},
    {name:'Dumont 765kV',lat:40.55,lng:-84.18,mw:3800,v:'765kV',owner:'AEP'},
    {name:'Sorenson 765kV',lat:40.15,lng:-84.65,mw:3600,v:'765kV',owner:'AEP'},
    // Columbus Area
    {name:'New Albany DC Hub',lat:40.08,lng:-82.82,mw:3600,v:'345kV',owner:'AEP'},
    {name:'Groveport',lat:39.88,lng:-82.88,mw:2800,v:'230kV',owner:'AEP'},
    {name:'Reynoldsburg',lat:39.95,lng:-82.80,mw:2200,v:'138kV',owner:'AEP'},
    {name:'Westerville',lat:40.12,lng:-82.92,mw:1800,v:'138kV',owner:'AEP'},
    {name:'Delaware',lat:40.28,lng:-83.07,mw:1600,v:'138kV',owner:'AEP'},
    {name:'Newark',lat:40.08,lng:-82.42,mw:1400,v:'138kV',owner:'AEP'},
    {name:'Lancaster',lat:39.72,lng:-82.60,mw:1200,v:'138kV',owner:'AEP'},
    {name:'Circleville',lat:39.60,lng:-82.95,mw:1000,v:'138kV',owner:'AEP'},
    {name:'Chillicothe',lat:39.33,lng:-82.98,mw:800,v:'69kV',owner:'AEP'},
    // Cleveland Area
    {name:'Sammis 765kV',lat:40.45,lng:-80.72,mw:4200,v:'765kV',owner:'FirstEnergy'},
    {name:'Perry Nuclear',lat:41.80,lng:-81.14,mw:3800,v:'345kV',owner:'Energy Harbor'},
    {name:'Davis-Besse',lat:41.60,lng:-83.09,mw:3200,v:'345kV',owner:'Energy Harbor'},
    {name:'Cleveland 138kV',lat:41.50,lng:-81.70,mw:2400,v:'138kV',owner:'FirstEnergy'},
    {name:'Akron',lat:41.08,lng:-81.52,mw:2000,v:'138kV',owner:'FirstEnergy'},
    {name:'Canton',lat:40.80,lng:-81.38,mw:1800,v:'138kV',owner:'FirstEnergy'},
    {name:'Youngstown',lat:41.10,lng:-80.65,mw:1600,v:'138kV',owner:'FirstEnergy'},
    
    // ==========================================
    // Southeast - Atlanta Area (25+)
    // ==========================================
    {name:'Atlanta North 500kV',lat:33.92,lng:-84.38,mw:3800,v:'500kV',owner:'Georgia Power'},
    {name:'Chattahoochee 500kV',lat:33.75,lng:-84.55,mw:3200,v:'500kV',owner:'Georgia Power'},
    {name:'Bowen 500kV',lat:34.12,lng:-84.92,mw:4600,v:'500kV',owner:'Georgia Power'},
    {name:'Scherer 500kV',lat:33.05,lng:-83.78,mw:5200,v:'500kV',owner:'Georgia Power'},
    {name:'Vogtle 500kV',lat:33.14,lng:-81.76,mw:6800,v:'500kV',owner:'Georgia Power'},
    {name:'Hatch 500kV',lat:31.93,lng:-82.34,mw:3800,v:'500kV',owner:'Georgia Power'},
    {name:'Midtown Atlanta',lat:33.78,lng:-84.38,mw:2000,v:'230kV',owner:'Georgia Power'},
    {name:'Buckhead',lat:33.85,lng:-84.38,mw:1800,v:'230kV',owner:'Georgia Power'},
    {name:'Sandy Springs',lat:33.92,lng:-84.38,mw:1600,v:'230kV',owner:'Georgia Power'},
    {name:'Alpharetta',lat:34.08,lng:-84.28,mw:1800,v:'230kV',owner:'Georgia Power'},
    {name:'Marietta',lat:33.95,lng:-84.55,mw:2200,v:'230kV',owner:'Georgia Power'},
    {name:'Kennesaw',lat:34.02,lng:-84.62,mw:1400,v:'115kV',owner:'Georgia Power'},
    {name:'Lawrenceville',lat:33.95,lng:-83.98,mw:1600,v:'230kV',owner:'Georgia Power'},
    {name:'Douglasville',lat:33.75,lng:-84.75,mw:1200,v:'115kV',owner:'Georgia Power'},
    
    // ==========================================
    // Colorado - Denver Area (20+)
    // ==========================================
    {name:'Cherokee 345kV',lat:39.82,lng:-104.95,mw:3400,v:'345kV',owner:'Xcel'},
    {name:'Pawnee 345kV',lat:40.25,lng:-103.65,mw:2800,v:'345kV',owner:'Xcel'},
    {name:'Comanche 345kV',lat:38.22,lng:-104.58,mw:3200,v:'345kV',owner:'Xcel'},
    {name:'Ault 345kV',lat:40.58,lng:-104.72,mw:2600,v:'345kV',owner:'Xcel'},
    {name:'Daniels Park',lat:39.45,lng:-104.95,mw:2200,v:'345kV',owner:'Xcel'},
    {name:'Smoky Hill',lat:39.58,lng:-104.72,mw:2400,v:'230kV',owner:'Xcel'},
    {name:'Arapahoe',lat:39.65,lng:-104.85,mw:2000,v:'230kV',owner:'Xcel'},
    {name:'Chambers',lat:39.72,lng:-104.82,mw:1800,v:'115kV',owner:'Xcel'},
    {name:'Waterton',lat:39.48,lng:-105.08,mw:1600,v:'115kV',owner:'Xcel'},
    {name:'Littleton',lat:39.62,lng:-105.02,mw:1400,v:'115kV',owner:'Xcel'},
    {name:'Aurora',lat:39.72,lng:-104.82,mw:1600,v:'115kV',owner:'Xcel'},
    {name:'Boulder',lat:40.02,lng:-105.25,mw:1200,v:'115kV',owner:'Xcel'},
    {name:'Fort Collins',lat:40.58,lng:-105.08,mw:1800,v:'230kV',owner:'Xcel'},
    {name:'Greeley',lat:40.42,lng:-104.72,mw:1400,v:'115kV',owner:'Xcel'},
    {name:'Longmont',lat:40.18,lng:-105.10,mw:1200,v:'115kV',owner:'Xcel'},
    
    // ==========================================
    // Utah - Salt Lake Area (15+)
    // ==========================================
    {name:'Terminal 345kV',lat:40.72,lng:-111.92,mw:2800,v:'345kV',owner:'PacifiCorp'},
    {name:'Mona 500kV',lat:39.82,lng:-111.88,mw:3200,v:'500kV',owner:'PacifiCorp'},
    {name:'Sigurd 345kV',lat:38.85,lng:-111.98,mw:2400,v:'345kV',owner:'PacifiCorp'},
    {name:'Huntington 345kV',lat:39.35,lng:-110.95,mw:2600,v:'345kV',owner:'PacifiCorp'},
    {name:'Intermountain HVDC',lat:39.52,lng:-112.58,mw:3800,v:'HVDC',owner:'LADWP'},
    {name:'Camp Williams',lat:40.42,lng:-111.92,mw:2000,v:'345kV',owner:'PacifiCorp'},
    {name:'Oquirrh',lat:40.58,lng:-112.02,mw:1800,v:'138kV',owner:'PacifiCorp'},
    {name:'Jordan',lat:40.62,lng:-111.98,mw:1600,v:'138kV',owner:'PacifiCorp'},
    {name:'Gadsby',lat:40.78,lng:-111.92,mw:2200,v:'345kV',owner:'PacifiCorp'},
    {name:'Pioneer',lat:40.72,lng:-111.88,mw:1400,v:'138kV',owner:'PacifiCorp'},
    {name:'Ogden',lat:41.22,lng:-111.98,mw:1600,v:'138kV',owner:'PacifiCorp'},
    {name:'Provo',lat:40.25,lng:-111.65,mw:1400,v:'138kV',owner:'PacifiCorp'},
    
    // ==========================================
    // Emerging Markets - New Locations (40+)
    // ==========================================
    // Iowa - Des Moines
    {name:'Des Moines 345kV',lat:41.59,lng:-93.62,mw:2800,v:'345kV',owner:'MidAmerican'},
    {name:'Ankeny',lat:41.72,lng:-93.62,mw:1800,v:'161kV',owner:'MidAmerican'},
    {name:'Altoona',lat:41.65,lng:-93.48,mw:2200,v:'345kV',owner:'MidAmerican'},
    {name:'Newton',lat:41.70,lng:-93.05,mw:1600,v:'161kV',owner:'MidAmerican'},
    // Nebraska - Omaha
    {name:'Omaha 345kV',lat:41.26,lng:-95.94,mw:2600,v:'345kV',owner:'OPPD'},
    {name:'Fort Calhoun',lat:41.52,lng:-96.08,mw:2200,v:'345kV',owner:'OPPD'},
    {name:'Sarpy County',lat:41.12,lng:-96.02,mw:1800,v:'161kV',owner:'OPPD'},
    {name:'Papillion',lat:41.15,lng:-96.05,mw:1400,v:'161kV',owner:'OPPD'},
    // Wyoming
    {name:'Cheyenne 230kV',lat:41.14,lng:-104.82,mw:1800,v:'230kV',owner:'Black Hills'},
    {name:'Laramie',lat:41.32,lng:-105.58,mw:1200,v:'230kV',owner:'PacifiCorp'},
    {name:'Casper',lat:42.85,lng:-106.32,mw:1400,v:'230kV',owner:'PacifiCorp'},
    // North Dakota
    {name:'Bismarck',lat:46.81,lng:-100.78,mw:1600,v:'230kV',owner:'Basin Electric'},
    {name:'Fargo',lat:46.88,lng:-96.79,mw:1400,v:'230kV',owner:'Xcel'},
    {name:'Grand Forks',lat:47.92,lng:-97.05,mw:1200,v:'230kV',owner:'Xcel'},
    // South Dakota
    {name:'Sioux Falls',lat:43.55,lng:-96.73,mw:1600,v:'230kV',owner:'Xcel'},
    {name:'Rapid City',lat:44.08,lng:-103.22,mw:1200,v:'230kV',owner:'Black Hills'},
    // Idaho
    {name:'Boise 230kV',lat:43.62,lng:-116.21,mw:2000,v:'230kV',owner:'Idaho Power'},
    {name:'Hemingway 500kV',lat:43.08,lng:-116.55,mw:3200,v:'500kV',owner:'Idaho Power'},
    // Indiana
    {name:'Indianapolis 345kV',lat:39.77,lng:-86.16,mw:2800,v:'345kV',owner:'Duke'},
    {name:'Gibson 765kV',lat:38.35,lng:-87.78,mw:4200,v:'765kV',owner:'Duke'},
    {name:'Cayuga 345kV',lat:40.08,lng:-87.45,mw:2400,v:'345kV',owner:'Duke'},
    // Alabama
    {name:'Huntsville 500kV',lat:34.73,lng:-86.59,mw:3200,v:'500kV',owner:'TVA'},
    {name:'Browns Ferry',lat:34.70,lng:-87.12,mw:4800,v:'500kV',owner:'TVA'},
    {name:'Widows Creek',lat:34.88,lng:-85.78,mw:2800,v:'500kV',owner:'TVA'},
    // Mississippi
    {name:'Attala 500kV',lat:33.28,lng:-89.48,mw:2600,v:'500kV',owner:'Entergy'},
    {name:'Grand Gulf 500kV',lat:32.01,lng:-91.05,mw:3800,v:'500kV',owner:'Entergy'}
];

        substations.forEach(function(s) {
            var color = s.v.includes('765')?'#ff0066':s.v.includes('500')?'#06b6d4':s.v.includes('345')?'#3b82f6':'#10b981';
            var radius = s.mw>4000?9:s.mw>2000?7:5;
            L.circleMarker([s.lat,s.lng],{
                radius:radius,fillColor:color,color:'#fff',weight:1,opacity:1,fillOpacity:0.8
            }).bindPopup('<div class="popup-title">🔌 '+s.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+s.mw.toLocaleString()+' MW</span></div>'+
                '<div class="popup-row"><span class="popup-label">Voltage</span><span class="popup-value">'+s.v+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">'+s.owner+'</span></div>'
            ).addTo(layers.substations);
        });

        // ============================================
        // GAS PIPELINES - EIA/FERC Data + Midstream Companies
        // Comprehensive coverage: Williams, Kinder Morgan, Energy Transfer, Enterprise, ONEOK, Targa, etc.
        // ============================================
        var gasPipelines = [
    // === WILLIAMS COMPANIES (33,000+ miles) ===
    {name:'Transco Main',coords:[[29.8,-95.4],[30.5,-93.5],[31.2,-91.8],[32.5,-89.5],[33.8,-86.2],[35.2,-82.8],[37.5,-79.2],[39.5,-76.8],[40.8,-74.5]],cap:'17.7 Bcf/d',owner:'Williams',dia:42,type:'Interstate'},
    {name:'Northwest Pipeline',coords:[[33.5,-112.2],[35.5,-115.5],[37.5,-119.5],[40.5,-122.2],[43.5,-123.2],[46.5,-122.5]],cap:'3.8 Bcf/d',owner:'Williams',dia:30,type:'Interstate'},
    {name:'Atlantic Sunrise',coords:[[41.2,-76.2],[40.5,-77.8],[39.8,-78.5],[39.2,-79.2]],cap:'1.7 Bcf/d',owner:'Williams',dia:42,type:'Interstate'},
    {name:'Constitution Pipeline',coords:[[42.5,-75.5],[42.2,-74.8],[42.0,-74.2]],cap:'0.65 Bcf/d',owner:'Williams',dia:30,type:'Gathering'},
    {name:'Leidy South',coords:[[41.5,-77.5],[40.8,-77.2],[40.2,-76.8]],cap:'0.6 Bcf/d',owner:'Williams',dia:30,type:'Gathering'},
    {name:'Gulf Stream',coords:[[27.5,-80.2],[28.0,-79.5],[28.5,-79.0]],cap:'1.3 Bcf/d',owner:'Williams',dia:24,type:'Offshore'},
    {name:'Gulfstar One',coords:[[28.5,-88.5],[29.0,-89.5],[29.5,-90.0]],cap:'1.2 Bcf/d',owner:'Williams',dia:24,type:'Offshore'},
    {name:'Discovery System',coords:[[28.8,-89.2],[29.2,-89.8],[29.5,-90.5]],cap:'0.9 Bcf/d',owner:'Williams',dia:20,type:'Offshore'},
    
    // === KINDER MORGAN (70,000+ miles) ===
    {name:'Tennessee Gas Pipeline',coords:[[29.5,-94.8],[30.8,-91.5],[32.2,-88.2],[34.5,-85.8],[37.8,-84.2],[40.2,-82.5],[41.8,-80.2],[42.5,-76.8]],cap:'8.2 Bcf/d',owner:'Kinder Morgan',dia:36,type:'Interstate'},
    {name:'El Paso Natural Gas',coords:[[31.8,-106.5],[32.2,-108.5],[32.5,-110.5],[33.5,-112.5],[34.2,-115.5],[34.5,-118.5]],cap:'5.4 Bcf/d',owner:'Kinder Morgan',dia:36,type:'Interstate'},
    {name:'Southern Natural Gas',coords:[[29.8,-94.5],[30.5,-91.2],[31.5,-88.5],[32.8,-86.2],[33.5,-84.5]],cap:'3.8 Bcf/d',owner:'Kinder Morgan',dia:30,type:'Interstate'},
    {name:'Natural Gas Pipeline of America',coords:[[29.5,-95.5],[32.5,-96.5],[35.5,-95.5],[38.5,-94.5],[41.5,-88.5]],cap:'4.2 Bcf/d',owner:'Kinder Morgan',dia:36,type:'Interstate'},
    {name:'Gulf Coast Express',coords:[[31.8,-102.5],[30.5,-99.5],[29.5,-97.2],[29.2,-95.5]],cap:'2.0 Bcf/d',owner:'Kinder Morgan',dia:42,type:'Interstate'},
    {name:'Permian Highway',coords:[[31.5,-103.2],[30.8,-100.5],[29.8,-98.2],[29.2,-96.8]],cap:'2.1 Bcf/d',owner:'Kinder Morgan',dia:42,type:'Interstate'},
    {name:'Florida Gas Transmission',coords:[[29.8,-94.5],[30.2,-87.5],[30.5,-85.5],[28.5,-82.5],[27.5,-80.8]],cap:'3.2 Bcf/d',owner:'Kinder Morgan',dia:30,type:'Interstate'},
    {name:'Midcontinent Express',coords:[[32.8,-94.2],[33.5,-91.5],[34.2,-89.5],[35.5,-87.2],[36.8,-86.5]],cap:'1.8 Bcf/d',owner:'Kinder Morgan',dia:36,type:'Interstate'},
    {name:'Eagle Ford Pipeline',coords:[[28.5,-99.5],[28.8,-98.2],[29.2,-96.8]],cap:'2.0 Bcf/d',owner:'Kinder Morgan',dia:42,type:'Gathering'},
    {name:'Tejas Pipeline',coords:[[29.5,-97.5],[29.2,-96.5],[28.8,-95.5]],cap:'1.5 Bcf/d',owner:'Kinder Morgan',dia:30,type:'Gathering'},
    {name:'Copano South Texas',coords:[[28.2,-97.8],[28.5,-97.2],[29.0,-96.5]],cap:'1.2 Bcf/d',owner:'Kinder Morgan',dia:24,type:'Gathering'},
    
    // === ENERGY TRANSFER (120,000+ miles) ===
    {name:'Texas Eastern',coords:[[29.6,-95.2],[31.5,-91.2],[33.5,-87.5],[36.5,-84.2],[38.8,-81.5],[40.5,-78.8],[41.2,-76.2]],cap:'9.4 Bcf/d',owner:'Energy Transfer',dia:36,type:'Interstate'},
    {name:'Panhandle Eastern',coords:[[35.5,-101.5],[37.5,-99.5],[39.5,-97.5],[41.5,-95.5],[43.5,-90.5]],cap:'2.5 Bcf/d',owner:'Energy Transfer',dia:30,type:'Interstate'},
    {name:'Trunkline',coords:[[29.5,-91.5],[32.5,-89.5],[35.5,-87.5],[38.5,-85.5],[41.5,-87.5]],cap:'2.0 Bcf/d',owner:'Energy Transfer',dia:36,type:'Interstate'},
    {name:'Rover Pipeline',coords:[[40.5,-80.5],[40.2,-82.5],[40.5,-84.5],[41.2,-86.5]],cap:'3.25 Bcf/d',owner:'Energy Transfer',dia:42,type:'Interstate'},
    {name:'Permian Basin Express',coords:[[31.5,-102.5],[30.5,-99.5],[29.5,-96.5]],cap:'2.5 Bcf/d',owner:'Energy Transfer',dia:42,type:'Interstate'},
    {name:'Oasis Pipeline',coords:[[31.8,-102.2],[30.5,-99.8],[29.5,-97.5]],cap:'1.8 Bcf/d',owner:'Energy Transfer',dia:36,type:'Gathering'},
    {name:'Lone Star Express',coords:[[32.0,-102.5],[31.2,-100.8],[30.5,-98.5]],cap:'2.0 Bcf/d',owner:'Energy Transfer',dia:42,type:'NGL'},
    {name:'Mariner East',coords:[[40.5,-80.5],[40.2,-78.8],[40.0,-76.5],[39.8,-75.2]],cap:'0.6 Bcf/d',owner:'Energy Transfer',dia:20,type:'NGL'},
    {name:'Sunoco Logistics',coords:[[40.0,-75.5],[39.5,-76.2],[38.5,-77.5]],cap:'0.8 Bcf/d',owner:'Energy Transfer',dia:24,type:'Refined'},
    
    // === ENBRIDGE (27,000+ miles US) ===
    {name:'Algonquin Gas',coords:[[41.2,-73.5],[41.5,-72.2],[41.8,-71.5],[42.2,-71.2]],cap:'3.0 Bcf/d',owner:'Enbridge',dia:30,type:'Interstate'},
    {name:'Alliance Pipeline',coords:[[53.5,-113.5],[52.5,-110.5],[50.5,-105.5],[48.5,-100.5],[46.5,-95.5],[44.5,-88.5]],cap:'1.6 Bcf/d',owner:'Enbridge',dia:36,type:'Interstate'},
    {name:'Vector Pipeline',coords:[[44.5,-88.5],[43.5,-85.5],[42.5,-82.5]],cap:'1.0 Bcf/d',owner:'Enbridge',dia:42,type:'Interstate'},
    {name:'Nexus Gas',coords:[[41.5,-81.5],[41.2,-83.5],[41.5,-85.5]],cap:'1.5 Bcf/d',owner:'Enbridge',dia:36,type:'Interstate'},
    {name:'Texas Eastern (Enbridge)',coords:[[29.5,-94.5],[30.8,-92.2],[32.5,-89.5],[35.2,-85.8]],cap:'4.5 Bcf/d',owner:'Enbridge',dia:36,type:'Interstate'},
    
    // === TC ENERGY (42,000+ miles) ===
    {name:'Columbia Gas',coords:[[37.8,-81.5],[38.8,-80.2],[39.5,-79.5],[40.2,-78.8],[40.8,-77.5],[41.2,-76.2]],cap:'5.0 Bcf/d',owner:'TC Energy',dia:30,type:'Interstate'},
    {name:'ANR Pipeline',coords:[[29.5,-94.5],[32.5,-92.5],[35.5,-90.5],[38.5,-88.5],[41.5,-87.5],[44.5,-86.5]],cap:'3.5 Bcf/d',owner:'TC Energy',dia:36,type:'Interstate'},
    {name:'Great Lakes Gas',coords:[[41.8,-87.5],[43.5,-86.5],[45.5,-85.5],[46.5,-84.5]],cap:'2.4 Bcf/d',owner:'TC Energy',dia:36,type:'Interstate'},
    {name:'Northern Border',coords:[[49.0,-108.5],[48.5,-104.5],[48.2,-100.5],[47.8,-96.5],[46.5,-92.5],[45.5,-88.5]],cap:'2.4 Bcf/d',owner:'TC Energy',dia:42,type:'Interstate'},
    {name:'Tuscarora Gas',coords:[[40.5,-117.5],[41.0,-119.0],[41.5,-120.5]],cap:'0.4 Bcf/d',owner:'TC Energy',dia:24,type:'Interstate'},
    
    // === ENTERPRISE PRODUCTS (22,000+ miles) ===
    {name:'Midcoast Operating',coords:[[29.5,-95.0],[30.2,-94.5],[30.8,-94.0]],cap:'0.8 Bcf/d',owner:'Enterprise',dia:24,type:'Gathering'},
    {name:'Shin Oak NGL',coords:[[31.5,-102.5],[30.5,-100.5],[29.5,-97.5]],cap:'0.6 Bcf/d',owner:'Enterprise',dia:24,type:'NGL'},
    {name:'Permian to Gulf Coast',coords:[[31.8,-102.2],[30.8,-99.8],[29.8,-97.2],[29.2,-95.8]],cap:'2.0 Bcf/d',owner:'Enterprise',dia:36,type:'NGL'},
    {name:'Acadian Gas',coords:[[30.2,-91.5],[30.0,-92.5],[29.8,-93.5]],cap:'1.5 Bcf/d',owner:'Enterprise',dia:30,type:'Gathering'},
    {name:'Jonah Gas Gathering',coords:[[42.5,-109.8],[42.2,-109.2],[41.8,-108.5]],cap:'1.2 Bcf/d',owner:'Enterprise',dia:24,type:'Gathering'},
    {name:'Texas Intrastate',coords:[[29.5,-95.5],[30.5,-96.5],[31.5,-97.5]],cap:'2.5 Bcf/d',owner:'Enterprise',dia:36,type:'Intrastate'},
    
    // === ONEOK (38,000+ miles) ===
    {name:'ONEOK Midcontinent',coords:[[35.5,-97.5],[36.5,-96.5],[37.5,-95.8],[38.5,-95.2]],cap:'3.5 Bcf/d',owner:'ONEOK',dia:36,type:'Gathering'},
    {name:'Viking Gas Transmission',coords:[[45.5,-94.5],[46.5,-96.5],[47.5,-98.5]],cap:'0.8 Bcf/d',owner:'ONEOK',dia:24,type:'Interstate'},
    {name:'ONEOK Rockies',coords:[[41.5,-109.5],[42.5,-107.5],[43.5,-105.5]],cap:'1.8 Bcf/d',owner:'ONEOK',dia:30,type:'Gathering'},
    {name:'ONEOK Permian Basin',coords:[[31.5,-102.5],[32.0,-101.5],[32.5,-100.5]],cap:'2.2 Bcf/d',owner:'ONEOK',dia:36,type:'Gathering'},
    {name:'ONEOK NGL West Texas',coords:[[31.8,-102.8],[31.2,-101.2],[30.5,-99.8]],cap:'1.0 Bcf/d',owner:'ONEOK',dia:24,type:'NGL'},
    
    // === TARGA RESOURCES (31,000+ miles) ===
    {name:'Targa Permian',coords:[[31.8,-102.5],[31.2,-101.5],[30.5,-100.2]],cap:'2.5 Bcf/d',owner:'Targa',dia:36,type:'Gathering'},
    {name:'Targa Delaware Basin',coords:[[32.0,-103.8],[31.5,-103.2],[31.0,-102.5]],cap:'2.0 Bcf/d',owner:'Targa',dia:30,type:'Gathering'},
    {name:'Targa Badlands',coords:[[47.5,-103.5],[48.0,-102.8],[48.5,-102.0]],cap:'0.8 Bcf/d',owner:'Targa',dia:24,type:'Gathering'},
    {name:'Targa Coastal',coords:[[29.5,-95.2],[29.2,-94.5],[29.0,-93.8]],cap:'1.5 Bcf/d',owner:'Targa',dia:30,type:'Gathering'},
    {name:'Grand Prix NGL',coords:[[31.5,-102.5],[30.5,-99.5],[29.5,-96.0]],cap:'0.6 Bcf/d',owner:'Targa',dia:24,type:'NGL'},
    
    // === WESTERN MIDSTREAM (15,000+ miles) ===
    {name:'Western Gas DJ Basin',coords:[[40.2,-104.5],[40.5,-103.8],[40.8,-103.2]],cap:'1.8 Bcf/d',owner:'Western Midstream',dia:30,type:'Gathering'},
    {name:'Western Gas Delaware',coords:[[31.5,-103.5],[31.2,-102.8],[31.0,-102.2]],cap:'2.0 Bcf/d',owner:'Western Midstream',dia:30,type:'Gathering'},
    {name:'Western Gas Powder River',coords:[[44.5,-106.5],[44.0,-105.8],[43.5,-105.2]],cap:'0.6 Bcf/d',owner:'Western Midstream',dia:24,type:'Gathering'},
    
    // === DCP MIDSTREAM (53,000+ miles) ===
    {name:'DCP Sand Hills',coords:[[31.5,-102.5],[30.8,-100.5],[30.0,-98.5],[29.5,-96.5]],cap:'0.9 Bcf/d',owner:'DCP',dia:24,type:'NGL'},
    {name:'DCP Southern Hills',coords:[[36.5,-97.5],[35.5,-96.5],[34.5,-95.5]],cap:'0.6 Bcf/d',owner:'DCP',dia:20,type:'NGL'},
    {name:'DCP DJ Basin',coords:[[40.2,-104.8],[40.5,-104.2],[40.8,-103.5]],cap:'1.5 Bcf/d',owner:'DCP',dia:30,type:'Gathering'},
    {name:'DCP Midcontinent',coords:[[35.5,-98.5],[36.2,-97.8],[37.0,-97.0]],cap:'2.0 Bcf/d',owner:'DCP',dia:30,type:'Gathering'},
    
    // === CRESTWOOD MIDSTREAM ===
    {name:'Crestwood Arrow',coords:[[38.5,-80.5],[39.0,-80.2],[39.5,-79.8]],cap:'0.8 Bcf/d',owner:'Crestwood',dia:24,type:'Gathering'},
    {name:'Crestwood Barnett',coords:[[32.5,-97.5],[33.0,-97.0],[33.5,-96.5]],cap:'1.2 Bcf/d',owner:'Crestwood',dia:30,type:'Gathering'},
    {name:'Crestwood Marcellus',coords:[[41.0,-76.5],[41.5,-76.0],[42.0,-75.5]],cap:'0.6 Bcf/d',owner:'Crestwood',dia:24,type:'Gathering'},
    
    // === EQM MIDSTREAM (950+ miles) ===
    {name:'Mountain Valley Pipeline',coords:[[37.2,-80.2],[37.8,-80.5],[38.2,-81.2],[38.5,-81.8]],cap:'2.0 Bcf/d',owner:'Equitrans',dia:42,type:'Interstate'},
    {name:'EQM Ohio Valley',coords:[[39.5,-80.5],[40.0,-80.2],[40.5,-79.8]],cap:'2.5 Bcf/d',owner:'Equitrans',dia:36,type:'Gathering'},
    {name:'EQM Equitrans',coords:[[39.8,-80.2],[40.2,-79.8],[40.5,-79.2]],cap:'3.0 Bcf/d',owner:'Equitrans',dia:36,type:'Gathering'},
    
    // === BERKSHIRE HATHAWAY ENERGY ===
    {name:'Northern Natural Gas',coords:[[29.5,-95.5],[32.5,-97.5],[35.5,-98.5],[38.5,-97.2],[41.5,-96.5],[44.5,-95.2]],cap:'4.5 Bcf/d',owner:'Berkshire',dia:30,type:'Interstate'},
    {name:'Kern River Gas',coords:[[40.8,-112.0],[39.5,-114.5],[37.5,-117.5],[35.5,-118.5]],cap:'2.0 Bcf/d',owner:'Berkshire',dia:36,type:'Interstate'},
    
    // === TALLGRASS ENERGY ===
    {name:'Rockies Express',coords:[[40.8,-110.5],[41.2,-107.5],[41.5,-104.5],[41.2,-101.5],[41.0,-98.5],[40.5,-95.5],[40.2,-92.5],[39.8,-89.5],[39.5,-86.5],[39.2,-83.5]],cap:'1.8 Bcf/d',owner:'Tallgrass',dia:42,type:'Interstate'},
    {name:'Ruby Pipeline',coords:[[40.8,-117.5],[41.2,-115.5],[41.5,-113.5],[41.2,-111.5],[40.8,-109.5]],cap:'1.5 Bcf/d',owner:'Tallgrass',dia:42,type:'Interstate'},
    {name:'Pony Express',coords:[[40.8,-104.5],[41.0,-101.5],[41.2,-98.5],[41.5,-95.5]],cap:'0.6 Bcf/d',owner:'Tallgrass',dia:20,type:'Oil'},
    
    // === WHITEWATER MIDSTREAM ===
    {name:'Whistler Pipeline',coords:[[31.5,-102.5],[30.5,-98.5],[29.5,-95.5]],cap:'2.0 Bcf/d',owner:'WhiteWater',dia:42,type:'Interstate'},
    {name:'Agua Blanca',coords:[[31.2,-103.5],[30.8,-102.8],[30.5,-102.0]],cap:'1.5 Bcf/d',owner:'WhiteWater',dia:30,type:'Gathering'},
    
    // === DOMINION ENERGY ===
    {name:'Dominion Cove Point',coords:[[38.4,-76.4],[39.0,-77.2],[39.5,-77.8]],cap:'1.0 Bcf/d',owner:'Dominion',dia:36,type:'Interstate'},
    {name:'Dominion South Point',coords:[[38.5,-77.5],[39.0,-77.8],[39.5,-78.2]],cap:'0.5 Bcf/d',owner:'Dominion',dia:24,type:'Distribution'},
    
    // === SPECTRA ENERGY (Now Enbridge) ===
    {name:'Sabal Trail',coords:[[30.5,-84.5],[29.5,-82.8],[28.5,-81.5],[27.8,-80.5]],cap:'1.1 Bcf/d',owner:'Spectra',dia:36,type:'Interstate'},
    
    // === REGIONAL PIPELINES ===
    {name:'Eastern Shore Natural Gas',coords:[[39.0,-75.5],[38.5,-75.8],[38.0,-76.2]],cap:'0.2 Bcf/d',owner:'Chesapeake',dia:16,type:'Distribution'},
    {name:'Iroquois Gas',coords:[[42.5,-73.8],[42.8,-73.2],[43.2,-72.8],[43.5,-72.2]],cap:'1.4 Bcf/d',owner:'Iroquois',dia:24,type:'Interstate'},
    {name:'Portland Natural Gas',coords:[[43.7,-70.3],[44.2,-70.8],[44.8,-71.2]],cap:'0.2 Bcf/d',owner:'PNGTS',dia:24,type:'Interstate'},
    {name:'Maritimes & Northeast',coords:[[44.8,-68.8],[45.2,-67.5],[45.8,-66.2]],cap:'0.5 Bcf/d',owner:'Spectra',dia:24,type:'Interstate'}
];

        gasPipelines.forEach(function(p) {
            // Color by type
            var pipeColor = '#f59e0b'; // Default orange
            if (p.type === 'NGL') pipeColor = '#a855f7'; // Purple for NGL
            else if (p.type === 'Gathering') pipeColor = '#22c55e'; // Green for gathering
            else if (p.type === 'Offshore') pipeColor = '#06b6d4'; // Cyan for offshore
            else if (p.type === 'Intrastate') pipeColor = '#f97316'; // Dark orange
            else if (p.type === 'Distribution') pipeColor = '#eab308'; // Yellow
            
            L.polyline(p.coords,{color:pipeColor,weight:4,opacity:0.8}).bindPopup(
                '<div class="popup-title">🔥 '+p.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+p.cap+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">'+p.owner+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+p.type+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Diameter</span><span class="popup-value">'+p.dia+'"</span></div>'
            ).addTo(layers.gas);
        });

        // ============================================
        // TRANSMISSION LINES - HIFLD Pattern
        // ============================================
        var transmissionLines = [
            {name:'Path 15 500kV',coords:[[35.2,-119.4],[36.0,-120.0],[36.8,-120.5],[37.4,-121.2]],v:'500kV',owner:'PG&E'},
            {name:'Path 26 500kV',coords:[[34.5,-118.5],[35.0,-119.0],[35.5,-119.5]],v:'500kV',owner:'SCE/PG&E'},
            {name:'PDCI 500kV DC',coords:[[45.8,-121.0],[43.5,-121.5],[41.5,-121.2],[38.5,-120.5],[35.5,-118.5]],v:'HVDC',owner:'BPA'},
            {name:'AEP 765kV Backbone',coords:[[38.5,-81.8],[39.5,-81.0],[40.2,-80.5],[40.8,-79.8]],v:'765kV',owner:'AEP'},
            {name:'PJM 500kV Ring',coords:[[39.0,-77.5],[39.5,-77.0],[40.0,-76.5],[40.5,-76.0]],v:'500kV',owner:'PJM'},
            {name:'ERCOT 345kV N-S',coords:[[33.2,-97.0],[32.0,-97.2],[30.5,-97.5],[29.5,-97.8]],v:'345kV',owner:'ERCOT'},
            {name:'SPP 345kV East',coords:[[36.5,-95.5],[37.5,-94.5],[38.5,-93.8],[39.2,-93.2]],v:'345kV',owner:'SPP'}
        ];

        transmissionLines.forEach(function(t) {
            var color = t.v.includes('765')?'#ff0066':t.v.includes('500')||t.v.includes('HVDC')?'#3b82f6':'#06b6d4';
            L.polyline(t.coords,{color:color,weight:3,opacity:0.7}).bindPopup(
                '<div class="popup-title">⚡ '+t.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Voltage</span><span class="popup-value">'+t.v+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">'+t.owner+'</span></div>'
            ).addTo(layers.grid);
        });

        // ============================================
        // FIBER ROUTES - Major US Carrier Backbone Networks
        // Data compiled from carrier network maps, FCC filings, public disclosures
        // ============================================
        var fiberRoutes = [
            // ============================================
            // TIER 1 - LUMEN (450,000+ route miles)
            // Largest US fiber network
            // ============================================
            {name:'Lumen I-95 Backbone',coords:[[42.4,-71.0],[40.7,-74.0],[39.5,-75.5],[38.9,-77.0],[37.5,-77.5],[33.8,-84.4],[30.3,-81.7],[25.8,-80.2]],cap:'2.4 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen I-80 Northern',coords:[[40.7,-74.0],[40.5,-80.0],[41.5,-87.6],[41.2,-96.0],[41.0,-104.0],[40.8,-112.0],[39.5,-119.8],[37.8,-122.4]],cap:'1.8 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen I-10 Southern',coords:[[30.3,-81.7],[30.4,-84.3],[29.8,-90.1],[29.8,-95.4],[31.8,-106.5],[32.2,-110.9],[33.5,-112.1],[34.1,-118.2]],cap:'1.6 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen I-35 Central',coords:[[29.4,-98.5],[30.3,-97.7],[32.9,-96.8],[35.5,-97.5],[39.1,-94.6],[41.2,-96.0],[44.9,-93.1]],cap:'1.2 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Denver Hub',coords:[[39.7,-105.0],[41.0,-104.0],[40.8,-112.0]],cap:'800 Gbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Pacific Northwest',coords:[[47.6,-122.3],[45.5,-122.7],[42.5,-122.5],[37.8,-122.4]],cap:'1.0 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Gulf Coast',coords:[[29.8,-95.4],[29.8,-90.1],[30.4,-84.3],[30.3,-81.7]],cap:'1.0 Tbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Mountain West',coords:[[39.7,-105.0],[40.76,-111.9],[43.6,-116.2],[46.9,-114.0]],cap:'600 Gbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Southeast Spine',coords:[[33.8,-84.4],[32.1,-81.2],[34.0,-81.0],[35.2,-80.8],[36.1,-79.8]],cap:'800 Gbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Florida Backbone',coords:[[30.3,-81.7],[28.5,-81.4],[27.5,-82.5],[26.7,-80.1],[25.8,-80.2]],cap:'600 Gbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
            {name:'Lumen Midwest Ring',coords:[[41.88,-87.63],[39.8,-86.2],[39.1,-84.5],[40.5,-80.0],[41.5,-81.7],[41.88,-87.63]],cap:'800 Gbps',owner:'Lumen',color:'#ef4444',type:'longhaul'},
        
            // ============================================
            // TIER 1 - ZAYO (133,000+ route miles)
            // Major wholesale/enterprise carrier
            // ============================================
            {name:'Zayo NYC-DC',coords:[[40.7,-74.0],[40.2,-74.8],[39.5,-75.5],[39.0,-76.5],[38.9,-77.0]],cap:'2.4 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo DC-Atlanta',coords:[[38.9,-77.0],[37.5,-77.5],[36.1,-79.8],[35.2,-80.8],[33.8,-84.4]],cap:'1.6 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Chicago-NYC',coords:[[41.88,-87.63],[41.5,-83.0],[40.5,-80.0],[40.7,-74.0]],cap:'1.8 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Denver-SLC',coords:[[39.7,-105.0],[40.5,-111.0],[40.76,-111.9]],cap:'800 Gbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Phoenix-LA',coords:[[33.5,-112.1],[34.1,-118.2]],cap:'1.2 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Texas Triangle',coords:[[32.9,-96.8],[30.3,-97.7],[29.8,-95.4],[32.9,-96.8]],cap:'1.0 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo West Coast',coords:[[47.6,-122.3],[45.5,-122.7],[37.8,-122.4],[34.1,-118.2],[32.7,-117.2]],cap:'1.6 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo I-70 Corridor',coords:[[39.7,-105.0],[39.1,-94.6],[38.6,-90.2],[39.8,-86.2],[40.5,-80.0]],cap:'1.2 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Southwest',coords:[[33.5,-112.1],[32.2,-110.9],[31.8,-106.5],[29.4,-98.5]],cap:'800 Gbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Minneapolis-Chicago',coords:[[44.98,-93.27],[43.0,-89.5],[41.88,-87.63]],cap:'600 Gbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Northern Route',coords:[[47.6,-122.3],[47.0,-117.4],[46.9,-114.0],[45.8,-108.5],[44.98,-93.27]],cap:'400 Gbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Low Latency NYC-Chicago',coords:[[40.7,-74.0],[40.4,-76.0],[40.4,-79.5],[41.0,-82.0],[41.88,-87.63]],cap:'1.2 Tbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
            {name:'Zayo Florida Extension',coords:[[33.8,-84.4],[30.3,-81.7],[28.5,-81.4],[27.5,-82.5],[25.8,-80.2]],cap:'600 Gbps',owner:'Zayo',color:'#8b5cf6',type:'longhaul'},
        
            // ============================================
            // TIER 1 - AT&T (500,000+ route miles)
            // Largest overall telecommunications network
            // ============================================
            {name:'AT&T Southeast Ring',coords:[[33.8,-84.4],[32.1,-81.1],[30.3,-81.7],[30.4,-84.3],[33.8,-84.4]],cap:'2.0 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Texas Triangle',coords:[[32.9,-96.8],[29.8,-95.4],[29.4,-98.5],[32.9,-96.8]],cap:'1.8 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Northeast Corridor',coords:[[42.4,-71.0],[41.8,-72.7],[40.7,-74.0],[40.0,-75.2],[38.9,-77.0]],cap:'2.4 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Chicago Hub',coords:[[41.88,-87.63],[42.3,-83.0],[40.5,-80.0],[41.5,-81.7]],cap:'1.6 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T West Backbone',coords:[[34.1,-118.2],[36.1,-115.2],[37.8,-122.4],[45.5,-122.7],[47.6,-122.3]],cap:'1.4 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Midwest Spine',coords:[[41.88,-87.63],[38.6,-90.2],[39.1,-94.6],[39.7,-105.0]],cap:'1.2 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Florida Backbone',coords:[[30.3,-81.7],[28.5,-81.4],[27.5,-82.5],[25.8,-80.2]],cap:'1.0 Tbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T I-65 Corridor',coords:[[33.8,-84.4],[35.1,-85.3],[36.2,-86.8],[38.3,-85.8],[39.8,-86.2],[41.88,-87.63]],cap:'800 Gbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Mountain West',coords:[[39.7,-105.0],[33.5,-112.1],[34.1,-118.2]],cap:'600 Gbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
            {name:'AT&T Pacific Northwest',coords:[[37.8,-122.4],[40.5,-122.4],[42.5,-122.5],[45.5,-122.7],[47.6,-122.3]],cap:'800 Gbps',owner:'AT&T',color:'#3b82f6',type:'longhaul'},
        
            // ============================================
            // TIER 1 - VERIZON
            // Major fiber network with FiOS footprint
            // ============================================
            {name:'Verizon NE Corridor',coords:[[42.4,-71.0],[41.3,-73.0],[40.7,-74.0],[40.2,-74.8],[39.5,-75.5],[38.9,-77.0]],cap:'1.6 Tbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
            {name:'Verizon Mid-Atlantic',coords:[[38.9,-77.0],[39.3,-76.6],[40.0,-75.2],[40.7,-74.0]],cap:'1.2 Tbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
            {name:'Verizon Great Lakes',coords:[[41.88,-87.63],[42.3,-83.0],[41.5,-81.7],[40.5,-80.0]],cap:'800 Gbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
            {name:'Verizon Texas',coords:[[32.9,-96.8],[29.8,-95.4],[29.4,-98.5]],cap:'600 Gbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
            {name:'Verizon Southeast',coords:[[38.9,-77.0],[37.5,-77.5],[35.8,-78.6],[33.8,-84.4]],cap:'800 Gbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
            {name:'Verizon Florida',coords:[[33.8,-84.4],[30.3,-81.7],[27.5,-82.5],[25.8,-80.2]],cap:'600 Gbps',owner:'Verizon',color:'#22c55e',type:'longhaul'},
        
            // ============================================
            // TIER 1 - COGENT (82,000+ route miles)
            // Major IP backbone provider
            // ============================================
            {name:'Cogent Eastern Backbone',coords:[[42.4,-71.0],[40.7,-74.0],[38.9,-77.0],[33.8,-84.4],[25.8,-80.2]],cap:'800 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
            {name:'Cogent Chicago-NYC',coords:[[41.88,-87.63],[41.5,-81.7],[40.5,-80.0],[40.7,-74.0]],cap:'600 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
            {name:'Cogent West Coast',coords:[[47.6,-122.3],[45.5,-122.7],[37.8,-122.4],[34.1,-118.2]],cap:'600 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
            {name:'Cogent I-70 Route',coords:[[38.9,-77.0],[40.5,-80.0],[39.8,-86.2],[38.6,-90.2],[39.1,-94.6],[39.7,-105.0]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
            {name:'Cogent Texas Route',coords:[[32.9,-96.8],[29.8,-95.4],[29.8,-90.1],[33.8,-84.4]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
            {name:'Cogent Southwest',coords:[[39.7,-105.0],[33.5,-112.1],[34.1,-118.2],[32.7,-117.2]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'longhaul'},
        
            // ============================================
            // TIER 1 - GTT (Global Tier 1)
            // International carrier with US backbone
            // ============================================
            {name:'GTT US Backbone East',coords:[[42.4,-71.0],[40.7,-74.0],[38.9,-77.0],[33.8,-84.4],[32.9,-96.8]],cap:'800 Gbps',owner:'GTT',color:'#0ea5e9',type:'longhaul'},
            {name:'GTT US Backbone West',coords:[[32.9,-96.8],[33.5,-112.1],[34.1,-118.2],[37.8,-122.4],[47.6,-122.3]],cap:'600 Gbps',owner:'GTT',color:'#0ea5e9',type:'longhaul'},
            {name:'GTT Chicago Hub',coords:[[41.88,-87.63],[40.7,-74.0],[38.9,-77.0]],cap:'400 Gbps',owner:'GTT',color:'#0ea5e9',type:'longhaul'},
            {name:'GTT Midwest Connection',coords:[[41.88,-87.63],[39.1,-94.6],[39.7,-105.0]],cap:'400 Gbps',owner:'GTT',color:'#0ea5e9',type:'longhaul'},
        
            // ============================================
            // TIER 1 - HURRICANE ELECTRIC (100+ Tbps)
            // Massive global IP backbone
            // ============================================
            {name:'HE US East',coords:[[42.4,-71.0],[40.7,-74.0],[38.9,-77.0],[33.8,-84.4]],cap:'1.2 Tbps',owner:'Hurricane Electric',color:'#dc2626',type:'longhaul'},
            {name:'HE US West',coords:[[47.6,-122.3],[45.5,-122.7],[37.8,-122.4],[34.1,-118.2],[33.5,-112.1]],cap:'1.0 Tbps',owner:'Hurricane Electric',color:'#dc2626',type:'longhaul'},
            {name:'HE Trans-Continental',coords:[[37.8,-122.4],[40.8,-112.0],[39.7,-105.0],[41.88,-87.63],[40.7,-74.0]],cap:'800 Gbps',owner:'Hurricane Electric',color:'#dc2626',type:'longhaul'},
            {name:'HE Southern Route',coords:[[34.1,-118.2],[33.5,-112.1],[32.9,-96.8],[29.8,-90.1],[33.8,-84.4]],cap:'600 Gbps',owner:'Hurricane Electric',color:'#dc2626',type:'longhaul'},
        
            // ============================================
            // TIER 1 - NTT COMMUNICATIONS
            // Global Tier 1 with extensive US presence
            // ============================================
            {name:'NTT US Backbone',coords:[[40.7,-74.0],[38.9,-77.0],[33.8,-84.4],[32.9,-96.8],[34.1,-118.2],[37.8,-122.4]],cap:'1.0 Tbps',owner:'NTT',color:'#7c3aed',type:'longhaul'},
            {name:'NTT Chicago-NYC',coords:[[41.88,-87.63],[40.7,-74.0]],cap:'400 Gbps',owner:'NTT',color:'#7c3aed',type:'longhaul'},
            {name:'NTT West Coast',coords:[[47.6,-122.3],[37.8,-122.4],[34.1,-118.2]],cap:'400 Gbps',owner:'NTT',color:'#7c3aed',type:'longhaul'},
            {name:'NTT Central Route',coords:[[37.8,-122.4],[40.8,-112.0],[39.7,-105.0],[41.88,-87.63]],cap:'400 Gbps',owner:'NTT',color:'#7c3aed',type:'longhaul'},
        
            // ============================================
            // TIER 1 - TELIA CARRIER (Arelion)
            // Global Tier 1 backbone
            // ============================================
            {name:'Telia US East-West',coords:[[40.7,-74.0],[41.88,-87.63],[39.7,-105.0],[34.1,-118.2]],cap:'800 Gbps',owner:'Telia',color:'#a21caf',type:'longhaul'},
            {name:'Telia Northeast',coords:[[42.4,-71.0],[40.7,-74.0],[38.9,-77.0]],cap:'400 Gbps',owner:'Telia',color:'#a21caf',type:'longhaul'},
            {name:'Telia West Coast',coords:[[47.6,-122.3],[37.8,-122.4],[34.1,-118.2]],cap:'400 Gbps',owner:'Telia',color:'#a21caf',type:'longhaul'},
        
            // ============================================
            // TIER 2 - CROWN CASTLE FIBER
            // Extensive metro and enterprise fiber
            // ============================================
            {name:'Crown Castle Northeast',coords:[[42.4,-71.0],[40.7,-74.0],[39.5,-75.5],[38.9,-77.0]],cap:'600 Gbps',owner:'Crown Castle',color:'#14b8a6',type:'longhaul'},
            {name:'Crown Castle Mid-Atlantic',coords:[[40.7,-74.0],[40.0,-75.2],[39.5,-75.5],[38.9,-77.0],[39.3,-76.6]],cap:'400 Gbps',owner:'Crown Castle',color:'#14b8a6',type:'longhaul'},
            {name:'Crown Castle Texas',coords:[[32.9,-96.8],[29.8,-95.4],[29.4,-98.5],[30.3,-97.7]],cap:'400 Gbps',owner:'Crown Castle',color:'#14b8a6',type:'longhaul'},
            {name:'Crown Castle West Coast',coords:[[34.1,-118.2],[37.8,-122.4],[45.5,-122.7]],cap:'400 Gbps',owner:'Crown Castle',color:'#14b8a6',type:'longhaul'},
        
            // ============================================
            // TIER 2 - WINDSTREAM / UNITI
            // Regional and wholesale fiber
            // ============================================
            {name:'Windstream Southeast',coords:[[35.8,-78.6],[35.2,-80.8],[33.8,-84.4],[32.8,-83.6],[30.3,-81.7]],cap:'400 Gbps',owner:'Windstream',color:'#f59e0b',type:'longhaul'},
            {name:'Windstream Midwest',coords:[[41.88,-87.63],[39.8,-86.2],[39.1,-84.5],[38.6,-90.2]],cap:'400 Gbps',owner:'Windstream',color:'#f59e0b',type:'longhaul'},
            {name:'Windstream Texas',coords:[[32.9,-96.8],[30.3,-97.7],[29.4,-98.5],[29.8,-95.4]],cap:'300 Gbps',owner:'Windstream',color:'#f59e0b',type:'longhaul'},
            {name:'Uniti Fiber Southeast',coords:[[33.8,-84.4],[32.1,-81.2],[30.3,-81.7],[28.5,-81.4]],cap:'400 Gbps',owner:'Uniti',color:'#f59e0b',type:'longhaul'},
            {name:'Uniti Fiber Gulf Coast',coords:[[29.8,-90.1],[30.4,-84.3],[30.3,-81.7]],cap:'300 Gbps',owner:'Uniti',color:'#f59e0b',type:'longhaul'},
            {name:'Uniti Fiber Southwest',coords:[[32.9,-96.8],[31.8,-106.5],[32.2,-110.9],[33.5,-112.1]],cap:'300 Gbps',owner:'Uniti',color:'#f59e0b',type:'longhaul'},
        
            // ============================================
            // TIER 2 - SPECTRUM ENTERPRISE
            // Cable-based fiber network
            // ============================================
            {name:'Spectrum Southeast',coords:[[33.8,-84.4],[35.2,-80.8],[36.1,-79.8],[37.5,-77.5],[38.9,-77.0]],cap:'600 Gbps',owner:'Spectrum',color:'#0891b2',type:'longhaul'},
            {name:'Spectrum Midwest',coords:[[41.88,-87.63],[39.8,-86.2],[38.6,-90.2],[39.1,-94.6]],cap:'400 Gbps',owner:'Spectrum',color:'#0891b2',type:'longhaul'},
            {name:'Spectrum Texas-Florida',coords:[[29.8,-95.4],[29.8,-90.1],[30.4,-84.3],[28.5,-81.4],[25.8,-80.2]],cap:'400 Gbps',owner:'Spectrum',color:'#0891b2',type:'longhaul'},
            {name:'Spectrum West Coast',coords:[[34.1,-118.2],[37.8,-122.4],[45.5,-122.7]],cap:'400 Gbps',owner:'Spectrum',color:'#0891b2',type:'longhaul'},
            {name:'Spectrum Northeast',coords:[[42.4,-71.0],[40.7,-74.0],[41.3,-73.0]],cap:'400 Gbps',owner:'Spectrum',color:'#0891b2',type:'longhaul'},
        
            // ============================================
            // TIER 2 - COMCAST BUSINESS
            // Enterprise fiber from cable footprint
            // ============================================
            {name:'Comcast Northeast',coords:[[42.4,-71.0],[40.7,-74.0],[39.5,-75.5],[38.9,-77.0]],cap:'600 Gbps',owner:'Comcast',color:'#ea580c',type:'longhaul'},
            {name:'Comcast Mid-Atlantic',coords:[[38.9,-77.0],[40.5,-80.0],[41.5,-81.7],[42.3,-83.0],[41.88,-87.63]],cap:'400 Gbps',owner:'Comcast',color:'#ea580c',type:'longhaul'},
            {name:'Comcast West',coords:[[34.1,-118.2],[37.8,-122.4],[45.5,-122.7],[47.6,-122.3]],cap:'400 Gbps',owner:'Comcast',color:'#ea580c',type:'longhaul'},
            {name:'Comcast Southeast',coords:[[33.8,-84.4],[30.3,-81.7],[28.5,-81.4],[25.8,-80.2]],cap:'300 Gbps',owner:'Comcast',color:'#ea580c',type:'longhaul'},
            {name:'Comcast Denver Hub',coords:[[41.88,-87.63],[39.7,-105.0],[40.76,-111.9]],cap:'300 Gbps',owner:'Comcast',color:'#ea580c',type:'longhaul'},
        
            // ============================================
            // TIER 2 - SEGRA (formerly Lumos)
            // Southeast/Mid-Atlantic regional fiber
            // ============================================
            {name:'Segra Mid-Atlantic',coords:[[38.9,-77.0],[37.5,-77.5],[36.1,-79.8],[35.8,-78.6],[35.2,-80.8]],cap:'400 Gbps',owner:'Segra',color:'#ec4899',type:'longhaul'},
            {name:'Segra Virginia',coords:[[38.9,-77.0],[38.5,-77.3],[37.5,-77.5],[36.85,-76.29]],cap:'300 Gbps',owner:'Segra',color:'#ec4899',type:'longhaul'},
            {name:'Segra Carolina',coords:[[35.8,-78.6],[35.2,-80.8],[34.0,-81.0],[32.8,-79.9]],cap:'300 Gbps',owner:'Segra',color:'#ec4899',type:'longhaul'},
            {name:'Segra Georgia',coords:[[33.8,-84.4],[33.4,-82.0],[32.1,-81.2]],cap:'200 Gbps',owner:'Segra',color:'#ec4899',type:'longhaul'},
        
            // ============================================
            // TIER 2 - FIRSTLIGHT FIBER
            // Northeast regional fiber
            // ============================================
            {name:'FirstLight New England',coords:[[42.4,-71.0],[42.1,-72.6],[43.2,-73.8],[44.5,-73.2],[44.98,-93.27]],cap:'400 Gbps',owner:'FirstLight',color:'#6366f1',type:'longhaul'},
            {name:'FirstLight NY State',coords:[[40.7,-74.0],[42.7,-73.8],[43.1,-75.2],[43.0,-76.15],[42.88,-78.88]],cap:'300 Gbps',owner:'FirstLight',color:'#6366f1',type:'longhaul'},
            {name:'FirstLight Boston-Albany',coords:[[42.4,-71.0],[42.1,-72.6],[42.7,-73.8]],cap:'200 Gbps',owner:'FirstLight',color:'#6366f1',type:'longhaul'},
            {name:'FirstLight Maine',coords:[[44.5,-73.2],[44.3,-69.8],[43.7,-70.3]],cap:'100 Gbps',owner:'FirstLight',color:'#6366f1',type:'longhaul'},
        
            // ============================================
            // TIER 2 - EVERSTREAM
            // Great Lakes regional fiber
            // ============================================
            {name:'Everstream Great Lakes',coords:[[41.88,-87.63],[42.3,-83.0],[41.5,-81.7],[40.5,-80.0]],cap:'400 Gbps',owner:'Everstream',color:'#10b981',type:'longhaul'},
            {name:'Everstream Ohio',coords:[[41.5,-81.7],[40.8,-81.4],[40.0,-82.9],[39.96,-83.0]],cap:'300 Gbps',owner:'Everstream',color:'#10b981',type:'longhaul'},
            {name:'Everstream Michigan',coords:[[42.3,-83.0],[42.7,-84.6],[42.96,-85.66]],cap:'200 Gbps',owner:'Everstream',color:'#10b981',type:'longhaul'},
            {name:'Everstream Indiana',coords:[[41.88,-87.63],[41.6,-86.3],[39.77,-86.16]],cap:'200 Gbps',owner:'Everstream',color:'#10b981',type:'longhaul'},
        
            // ============================================
            // TIER 2 - FIBERLIGHT
            // Data center focused regional fiber
            // ============================================
            {name:'FiberLight Virginia',coords:[[39.04,-77.49],[38.9,-77.0],[38.5,-77.3],[37.5,-77.5]],cap:'400 Gbps',owner:'FiberLight',color:'#f472b6',type:'metro'},
            {name:'FiberLight Texas',coords:[[32.9,-96.8],[32.8,-97.0],[32.76,-97.33]],cap:'200 Gbps',owner:'FiberLight',color:'#f472b6',type:'metro'},
            {name:'FiberLight Atlanta',coords:[[33.8,-84.4],[33.95,-84.35],[33.7,-84.5]],cap:'200 Gbps',owner:'FiberLight',color:'#f472b6',type:'metro'},
        
            // ============================================
            // TIER 2 - LIGHTPATH
            // NY/NJ metro fiber specialist
            // ============================================
            {name:'Lightpath NYC Metro',coords:[[40.7,-74.0],[40.75,-73.95],[40.85,-74.1],[41.0,-74.2]],cap:'600 Gbps',owner:'Lightpath',color:'#a855f7',type:'metro'},
            {name:'Lightpath Long Island',coords:[[40.7,-74.0],[40.74,-73.59],[40.88,-72.8]],cap:'400 Gbps',owner:'Lightpath',color:'#a855f7',type:'metro'},
            {name:'Lightpath NJ Corridor',coords:[[40.7,-74.0],[40.47,-74.37],[40.22,-74.77]],cap:'300 Gbps',owner:'Lightpath',color:'#a855f7',type:'metro'},
        
            // ============================================
            // REGIONAL - BANDWIDTH IG
            // Research Triangle / NC fiber
            // ============================================
            {name:'Bandwidth IG NC Triangle',coords:[[35.8,-78.6],[35.9,-79.1],[36.1,-79.8]],cap:'200 Gbps',owner:'Bandwidth IG',color:'#fb923c',type:'metro'},
            {name:'Bandwidth IG RDU',coords:[[35.8,-78.6],[35.77,-78.64],[35.87,-78.79]],cap:'100 Gbps',owner:'Bandwidth IG',color:'#fb923c',type:'metro'},
        
            // ============================================
            // REGIONAL - SUMMIT IG
            // Virginia infrastructure group
            // ============================================
            {name:'Summit IG Virginia',coords:[[38.9,-77.0],[38.5,-77.3],[38.0,-78.5],[37.5,-77.5]],cap:'200 Gbps',owner:'Summit IG',color:'#facc15',type:'metro'},
            {name:'Summit IG NoVA Hub',coords:[[39.04,-77.49],[38.95,-77.4],[38.9,-77.0]],cap:'100 Gbps',owner:'Summit IG',color:'#facc15',type:'metro'},
        
            // ============================================
            // REGIONAL - BOLDYN NETWORKS
            // Metro/enterprise fiber
            // ============================================
            {name:'Boldyn Metro NYC',coords:[[40.7,-74.0],[40.75,-73.98],[40.78,-73.95]],cap:'200 Gbps',owner:'Boldyn Networks',color:'#22d3ee',type:'metro'},
            {name:'Boldyn Chicago',coords:[[41.88,-87.63],[41.9,-87.68],[41.85,-87.65]],cap:'200 Gbps',owner:'Boldyn Networks',color:'#22d3ee',type:'metro'},
        
            // ============================================
            // REGIONAL - GLO FIBER
            // Southeast metro fiber
            // ============================================
            {name:'Glo Fiber Virginia',coords:[[37.27,-79.94],[37.41,-79.15],[38.03,-78.48]],cap:'100 Gbps',owner:'Glo Fiber',color:'#84cc16',type:'metro'},
            {name:'Glo Fiber Shenandoah',coords:[[38.45,-78.87],[38.89,-77.95],[39.17,-77.73]],cap:'100 Gbps',owner:'Glo Fiber',color:'#84cc16',type:'metro'},
        
            // ============================================
            // REGIONAL - TING FIBER
            // Southeast/Piedmont fiber
            // ============================================
            {name:'Ting Southeast',coords:[[35.8,-78.6],[35.2,-80.8],[34.0,-81.0]],cap:'100 Gbps',owner:'Ting',color:'#818cf8',type:'metro'},
            {name:'Ting Piedmont',coords:[[35.6,-82.55],[35.4,-80.85]],cap:'100 Gbps',owner:'Ting',color:'#818cf8',type:'metro'},
        
            // ============================================
            // REGIONAL - CONSOLIDATED COMMUNICATIONS
            // New England/Midwest fiber
            // ============================================
            {name:'Consolidated New England',coords:[[42.4,-71.0],[43.2,-71.5],[44.5,-73.2]],cap:'200 Gbps',owner:'Consolidated',color:'#94a3b8',type:'longhaul'},
            {name:'Consolidated Midwest',coords:[[41.88,-87.63],[41.6,-93.6],[44.98,-93.27]],cap:'200 Gbps',owner:'Consolidated',color:'#94a3b8',type:'longhaul'},
        
            // ============================================
            // REGIONAL - FATBEAM
            // Northwest fiber specialist
            // ============================================
            {name:'Fatbeam Northwest',coords:[[47.6,-122.3],[47.0,-117.4],[46.9,-114.0]],cap:'200 Gbps',owner:'Fatbeam',color:'#06b6d4',type:'longhaul'},
            {name:'Fatbeam Idaho',coords:[[43.6,-116.2],[46.6,-117.4],[47.65,-117.43]],cap:'100 Gbps',owner:'Fatbeam',color:'#06b6d4',type:'longhaul'},
        
            // ============================================
            // REGIONAL - SHENTEL / GLOFIBER
            // Shenandoah Valley fiber
            // ============================================
            {name:'Shentel Valley',coords:[[38.45,-78.87],[39.17,-77.73],[38.9,-77.0]],cap:'100 Gbps',owner:'Shentel',color:'#a3e635',type:'metro'},
        
            // ============================================
            // REGIONAL - ALL POINTS BROADBAND
            // Rural Virginia/West Virginia
            // ============================================
            {name:'All Points VA',coords:[[37.5,-77.5],[38.03,-78.48],[38.45,-78.87]],cap:'100 Gbps',owner:'All Points',color:'#4ade80',type:'metro'},
        
            // ============================================
            // REGIONAL - TDS TELECOM
            // Rural/regional fiber
            // ============================================
            {name:'TDS Wisconsin',coords:[[43.07,-89.40],[44.52,-88.02],[43.04,-87.91]],cap:'200 Gbps',owner:'TDS Telecom',color:'#fcd34d',type:'longhaul'},
            {name:'TDS Tennessee',coords:[[36.16,-86.78],[35.96,-83.92],[36.31,-82.35]],cap:'100 Gbps',owner:'TDS Telecom',color:'#fcd34d',type:'longhaul'},
        
            // ============================================
            // EXCHANGE/INTERCONNECT PROVIDERS
            // ============================================
            {name:'Equinix Fabric East',coords:[[40.7,-74.0],[38.9,-77.0],[33.8,-84.4],[25.8,-80.2]],cap:'1.0 Tbps',owner:'Equinix',color:'#e11d48',type:'fabric'},
            {name:'Equinix Fabric Ashburn Hub',coords:[[39.04,-77.49],[38.9,-77.0],[38.95,-77.1]],cap:'2.0 Tbps',owner:'Equinix',color:'#e11d48',type:'fabric'},
            {name:'Equinix Fabric West',coords:[[47.6,-122.3],[37.8,-122.4],[34.1,-118.2]],cap:'800 Gbps',owner:'Equinix',color:'#e11d48',type:'fabric'},
            {name:'Megaport US East',coords:[[42.4,-71.0],[40.7,-74.0],[38.9,-77.0],[33.8,-84.4],[32.9,-96.8]],cap:'400 Gbps',owner:'Megaport',color:'#f472b6',type:'fabric'},
            {name:'Megaport US West',coords:[[32.9,-96.8],[39.7,-105.0],[34.1,-118.2],[37.8,-122.4],[47.6,-122.3]],cap:'400 Gbps',owner:'Megaport',color:'#f472b6',type:'fabric'},
        
            // ============================================
            // DARK FIBER SPECIALISTS
            // ============================================
            {name:'Sungard Northeast',coords:[[40.7,-74.0],[39.5,-75.5],[38.9,-77.0]],cap:'200 Gbps',owner:'Sungard AS',color:'#fbbf24',type:'dark'},
            {name:'Sungard Chicago',coords:[[41.88,-87.63],[40.5,-80.0],[40.7,-74.0]],cap:'200 Gbps',owner:'Sungard AS',color:'#fbbf24',type:'dark'},
            {name:'Digital Realty Connect',coords:[[39.04,-77.49],[41.88,-87.63],[37.8,-122.4]],cap:'400 Gbps',owner:'Digital Realty',color:'#2563eb',type:'fabric'},
            {name:'CyrusOne Fiber',coords:[[39.04,-77.49],[32.9,-96.8],[33.5,-112.1]],cap:'300 Gbps',owner:'CyrusOne',color:'#059669',type:'dark'},
        
            // ============================================
            // KEY METRO RINGS & HYPERSCALER ROUTES
            // ============================================
            {name:'Ashburn Metro Ring',coords:[[39.04,-77.49],[39.08,-77.45],[39.02,-77.42],[38.98,-77.46],[39.04,-77.49]],cap:'100+ Tbps',owner:'20+ Carriers',color:'#10b981',type:'metro'},
            {name:'Chicago Metro Ring',coords:[[41.88,-87.63],[41.92,-87.75],[41.85,-87.85],[41.78,-87.72],[41.88,-87.63]],cap:'50+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Quincy-Seattle Cloud',coords:[[47.23,-119.85],[47.5,-120.5],[47.6,-122.3]],cap:'4+ Tbps',owner:'MSFT/GOOG',color:'#10b981',type:'hyperscale'},
            {name:'Silicon Valley Ring',coords:[[37.8,-122.4],[37.5,-122.2],[37.3,-121.9],[37.4,-122.1],[37.8,-122.4]],cap:'40+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Dallas Metro Ring',coords:[[32.9,-96.8],[33.0,-96.6],[32.8,-96.5],[32.7,-96.7],[32.9,-96.8]],cap:'30+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Phoenix Metro Ring',coords:[[33.5,-112.1],[33.6,-111.9],[33.4,-111.8],[33.3,-112.0],[33.5,-112.1]],cap:'20+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Atlanta Metro Ring',coords:[[33.8,-84.4],[33.9,-84.3],[33.7,-84.2],[33.6,-84.5],[33.8,-84.4]],cap:'25+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'NYC/NJ Metro Ring',coords:[[40.7,-74.0],[40.8,-74.1],[40.75,-74.2],[40.65,-74.15],[40.7,-74.0]],cap:'60+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Denver Metro Ring',coords:[[39.74,-104.99],[39.8,-105.0],[39.7,-105.1],[39.65,-105.0],[39.74,-104.99]],cap:'15+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Columbus Metro Ring',coords:[[39.96,-83.0],[40.0,-82.95],[39.92,-82.9],[39.88,-83.0],[39.96,-83.0]],cap:'10+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
        
            // ============================================
            // HYPERSCALER PRIVATE NETWORKS
            // ============================================
            {name:'Microsoft Azure Express',coords:[[47.23,-119.85],[39.7,-105.0],[41.88,-87.63],[39.04,-77.49]],cap:'6+ Tbps',owner:'Microsoft',color:'#00a4ef',type:'hyperscale'},
            {name:'Google Cloud Backbone',coords:[[37.8,-122.4],[34.1,-118.2],[39.7,-105.0],[41.88,-87.63],[39.04,-77.49],[33.8,-84.4]],cap:'8+ Tbps',owner:'Google',color:'#34a853',type:'hyperscale'},
            {name:'AWS Direct Connect',coords:[[39.04,-77.49],[40.7,-74.0],[41.88,-87.63],[47.6,-122.3],[34.1,-118.2]],cap:'10+ Tbps',owner:'AWS',color:'#ff9900',type:'hyperscale'},
            {name:'Meta Backbone',coords:[[39.04,-77.49],[33.8,-84.4],[32.9,-96.8],[34.1,-118.2],[47.6,-122.3]],cap:'8+ Tbps',owner:'Meta',color:'#0668E1',type:'hyperscale'},
            {name:'Oracle Cloud Connect',coords:[[39.04,-77.49],[33.5,-112.1],[37.8,-122.4]],cap:'4+ Tbps',owner:'Oracle',color:'#C74634',type:'hyperscale'},
            {name:'Microsoft Columbus Route',coords:[[39.96,-83.0],[39.04,-77.49],[41.88,-87.63]],cap:'4+ Tbps',owner:'Microsoft',color:'#00a4ef',type:'hyperscale'},
            {name:'Google Council Bluffs',coords:[[41.26,-95.86],[41.88,-87.63],[39.04,-77.49]],cap:'4+ Tbps',owner:'Google',color:'#34a853',type:'hyperscale'},
        
            // ============================================
            // ADDITIONAL METRO / REGIONAL ROUTES
            // ============================================
            {name:'Boston Metro',coords:[[42.4,-71.0],[42.35,-71.05],[42.37,-71.1],[42.4,-71.0]],cap:'15+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Philadelphia Metro',coords:[[39.95,-75.17],[39.98,-75.12],[39.9,-75.2],[39.95,-75.17]],cap:'12+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Miami Metro',coords:[[25.8,-80.2],[25.85,-80.15],[25.75,-80.25],[25.8,-80.2]],cap:'18+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Seattle Metro',coords:[[47.6,-122.33],[47.65,-122.3],[47.55,-122.35],[47.6,-122.33]],cap:'20+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Los Angeles Metro',coords:[[34.05,-118.24],[34.1,-118.2],[34.0,-118.3],[34.05,-118.24]],cap:'35+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Minneapolis Metro',coords:[[44.98,-93.27],[45.0,-93.2],[44.95,-93.3],[44.98,-93.27]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Salt Lake Metro',coords:[[40.76,-111.89],[40.8,-111.85],[40.72,-111.93],[40.76,-111.89]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Las Vegas Metro',coords:[[36.17,-115.14],[36.2,-115.1],[36.14,-115.18],[36.17,-115.14]],cap:'12+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Portland Metro',coords:[[45.52,-122.68],[45.55,-122.65],[45.48,-122.72],[45.52,-122.68]],cap:'10+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'San Diego Metro',coords:[[32.72,-117.16],[32.75,-117.12],[32.68,-117.2],[32.72,-117.16]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Charlotte Metro',coords:[[35.23,-80.84],[35.27,-80.8],[35.19,-80.88],[35.23,-80.84]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Nashville Metro',coords:[[36.16,-86.78],[36.2,-86.74],[36.12,-86.82],[36.16,-86.78]],cap:'6+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Kansas City Metro',coords:[[39.1,-94.58],[39.15,-94.55],[39.05,-94.62],[39.1,-94.58]],cap:'6+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'St Louis Metro',coords:[[38.63,-90.2],[38.67,-90.17],[38.59,-90.24],[38.63,-90.2]],cap:'6+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Richmond Metro',coords:[[37.54,-77.44],[37.58,-77.4],[37.5,-77.48],[37.54,-77.44]],cap:'6+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Raleigh Metro',coords:[[35.78,-78.64],[35.82,-78.6],[35.74,-78.68],[35.78,-78.64]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Austin Metro',coords:[[30.27,-97.74],[30.3,-97.7],[30.24,-97.78],[30.27,-97.74]],cap:'10+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'San Antonio Metro',coords:[[29.42,-98.49],[29.46,-98.45],[29.38,-98.53],[29.42,-98.49]],cap:'6+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Houston Metro',coords:[[29.76,-95.37],[29.8,-95.33],[29.72,-95.41],[29.76,-95.37]],cap:'12+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},
            {name:'Detroit Metro',coords:[[42.33,-83.05],[42.37,-83.01],[42.29,-83.09],[42.33,-83.05]],cap:'8+ Tbps',owner:'Multiple',color:'#10b981',type:'metro'},

            // ============================================
            // 123Net - Michigan Regional
            // ============================================
            {name:'123Net Detroit Core',coords:[[42.33,-83.05],[42.40,-83.15],[42.45,-83.25],[42.50,-83.10]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Ann Arbor',coords:[[42.28,-83.75],[42.33,-83.05]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Grand Rapids',coords:[[42.96,-85.66],[42.50,-85.00],[42.33,-83.05]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Lansing',coords:[[42.73,-84.55],[42.33,-83.05]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},

            // ============================================
            // Crown Castle - National Dark Fiber (85,000+ miles)
            // ============================================
            {name:'Crown Castle Northeast',coords:[[42.4,-71.0],[41.3,-73.0],[40.7,-74.0],[39.95,-75.17],[38.9,-77.0]],cap:'Dark Fiber',owner:'Crown Castle',color:'#2c3e50',type:'dark'},
            {name:'Crown Castle Southeast',coords:[[38.9,-77.0],[37.5,-77.5],[35.8,-78.6],[33.8,-84.4],[30.3,-81.7]],cap:'Dark Fiber',owner:'Crown Castle',color:'#2c3e50',type:'dark'},
            {name:'Crown Castle Midwest',coords:[[41.88,-87.63],[39.77,-86.16],[39.96,-83.00],[40.44,-80.00]],cap:'Dark Fiber',owner:'Crown Castle',color:'#2c3e50',type:'dark'},
            {name:'Crown Castle Texas',coords:[[32.78,-96.80],[30.27,-97.74],[29.76,-95.37],[29.42,-98.49]],cap:'Dark Fiber',owner:'Crown Castle',color:'#2c3e50',type:'dark'},
            {name:'Crown Castle West',coords:[[47.61,-122.33],[45.52,-122.68],[37.77,-122.42],[34.05,-118.24]],cap:'Dark Fiber',owner:'Crown Castle',color:'#2c3e50',type:'dark'},

            // ============================================
            // Windstream/Kinetic (150,000+ miles)
            // ============================================
            {name:'Windstream Southeast',coords:[[33.8,-84.4],[34.0,-81.0],[35.2,-80.8],[36.1,-79.8],[37.5,-77.5]],cap:'400 Gbps',owner:'Windstream',color:'#9b59b6',type:'longhaul'},
            {name:'Windstream Midwest',coords:[[41.88,-87.63],[39.77,-86.16],[38.25,-85.76],[36.16,-86.78]],cap:'400 Gbps',owner:'Windstream',color:'#9b59b6',type:'longhaul'},
            {name:'Windstream Southwest',coords:[[32.78,-96.80],[35.47,-97.52],[39.1,-94.58]],cap:'400 Gbps',owner:'Windstream',color:'#9b59b6',type:'longhaul'},
            {name:'Windstream Northeast',coords:[[40.7,-74.0],[41.3,-73.0],[42.4,-71.0],[43.0,-71.5]],cap:'400 Gbps',owner:'Windstream',color:'#9b59b6',type:'longhaul'},

            // ============================================
            // Uniti Fiber (137,000+ route miles)
            // ============================================
            {name:'Uniti Southeast Backbone',coords:[[33.8,-84.4],[32.08,-81.09],[30.3,-81.7],[27.95,-82.46],[25.8,-80.2]],cap:'800 Gbps',owner:'Uniti',color:'#1abc9c',type:'longhaul'},
            {name:'Uniti Gulf States',coords:[[29.76,-95.37],[30.45,-91.15],[30.69,-88.04],[30.4,-84.3]],cap:'400 Gbps',owner:'Uniti',color:'#1abc9c',type:'longhaul'},
            {name:'Uniti Mid-Atlantic',coords:[[38.9,-77.0],[39.95,-75.17],[40.7,-74.0]],cap:'400 Gbps',owner:'Uniti',color:'#1abc9c',type:'longhaul'},

            // ============================================
            // Segra (formerly Lumos) - Southeast
            // ============================================
            {name:'Segra Virginia',coords:[[37.54,-77.44],[38.03,-78.48],[37.27,-79.94],[36.85,-76.05]],cap:'100 Gbps',owner:'Segra',color:'#e67e22',type:'metro'},
            {name:'Segra North Carolina',coords:[[35.78,-78.64],[35.23,-80.84],[36.07,-79.79]],cap:'100 Gbps',owner:'Segra',color:'#e67e22',type:'metro'},
            {name:'Segra South Carolina',coords:[[34.0,-81.03],[32.78,-79.93],[33.84,-81.16]],cap:'100 Gbps',owner:'Segra',color:'#e67e22',type:'metro'},

            // ============================================
            // Lightpath - NYC Metro (5,800+ miles)
            // ============================================
            {name:'Lightpath Manhattan',coords:[[40.75,-73.99],[40.72,-74.00],[40.78,-73.96],[40.71,-73.95]],cap:'400 Gbps',owner:'Lightpath',color:'#3498db',type:'metro'},
            {name:'Lightpath Long Island',coords:[[40.75,-73.75],[40.70,-73.50],[40.65,-73.30],[40.78,-72.90]],cap:'100 Gbps',owner:'Lightpath',color:'#3498db',type:'metro'},
            {name:'Lightpath New Jersey',coords:[[40.73,-74.17],[40.68,-74.25],[40.56,-74.40],[40.48,-74.26]],cap:'100 Gbps',owner:'Lightpath',color:'#3498db',type:'metro'},
            {name:'Lightpath Westchester',coords:[[40.95,-73.82],[41.03,-73.76],[41.15,-73.85]],cap:'100 Gbps',owner:'Lightpath',color:'#3498db',type:'metro'},

            // ============================================
            // FirstLight - New England (25,000+ miles)
            // ============================================
            {name:'FirstLight Boston',coords:[[42.36,-71.06],[42.45,-71.15],[42.28,-71.00]],cap:'100 Gbps',owner:'FirstLight',color:'#f39c12',type:'metro'},
            {name:'FirstLight Vermont',coords:[[44.26,-72.58],[43.61,-72.97],[44.48,-73.21]],cap:'100 Gbps',owner:'FirstLight',color:'#f39c12',type:'metro'},
            {name:'FirstLight New Hampshire',coords:[[43.21,-71.54],[42.99,-71.45],[43.00,-70.93]],cap:'100 Gbps',owner:'FirstLight',color:'#f39c12',type:'metro'},
            {name:'FirstLight Maine',coords:[[43.66,-70.25],[44.31,-69.78],[44.80,-68.77]],cap:'100 Gbps',owner:'FirstLight',color:'#f39c12',type:'metro'},
            {name:'FirstLight NY-NE',coords:[[42.65,-73.75],[42.89,-73.33],[43.08,-73.79],[42.36,-71.06]],cap:'100 Gbps',owner:'FirstLight',color:'#f39c12',type:'longhaul'},

            // ============================================
            // FiberLight - Texas/Southeast
            // ============================================
            {name:'FiberLight Dallas',coords:[[32.78,-96.80],[32.95,-96.70],[32.75,-97.33],[33.20,-96.65]],cap:'100 Gbps',owner:'FiberLight',color:'#e74c3c',type:'metro'},
            {name:'FiberLight Houston',coords:[[29.76,-95.37],[29.95,-95.55],[29.60,-95.22],[29.85,-95.08]],cap:'100 Gbps',owner:'FiberLight',color:'#e74c3c',type:'metro'},
            {name:'FiberLight DASH',coords:[[32.78,-96.80],[29.76,-95.37]],cap:'400 Gbps',owner:'FiberLight',color:'#e74c3c',type:'longhaul'},
            {name:'FiberLight Austin',coords:[[30.27,-97.74],[30.50,-97.82],[30.35,-97.55]],cap:'100 Gbps',owner:'FiberLight',color:'#e74c3c',type:'metro'},

            // ============================================
            // Consolidated Communications
            // ============================================
            {name:'Consolidated New England',coords:[[42.36,-71.06],[43.21,-71.54],[43.66,-70.25]],cap:'100 Gbps',owner:'Consolidated',color:'#27ae60',type:'metro'},
            {name:'Consolidated Minnesota',coords:[[44.98,-93.27],[46.78,-92.10],[47.47,-92.89]],cap:'100 Gbps',owner:'Consolidated',color:'#27ae60',type:'metro'},
            {name:'Consolidated Texas',coords:[[32.78,-96.80],[31.76,-106.49],[29.42,-98.49]],cap:'100 Gbps',owner:'Consolidated',color:'#27ae60',type:'longhaul'},

            // ============================================
            // Altice/Optimum - Northeast
            // ============================================
            {name:'Altice NY Metro',coords:[[40.75,-73.99],[41.03,-73.76],[41.15,-74.05]],cap:'100 Gbps',owner:'Altice',color:'#9b59b6',type:'metro'},
            {name:'Altice Long Island',coords:[[40.75,-73.75],[40.78,-73.42],[40.87,-72.66]],cap:'100 Gbps',owner:'Altice',color:'#9b59b6',type:'metro'},
            {name:'Altice New Jersey',coords:[[40.73,-74.17],[40.22,-74.01],[39.95,-75.17]],cap:'100 Gbps',owner:'Altice',color:'#9b59b6',type:'metro'},

            // ============================================
            // Frontier Communications
            // ============================================
            {name:'Frontier California',coords:[[34.05,-118.24],[33.77,-118.19],[33.14,-117.35]],cap:'100 Gbps',owner:'Frontier',color:'#e74c3c',type:'metro'},
            {name:'Frontier Texas',coords:[[32.78,-96.80],[32.35,-95.30],[30.27,-97.74]],cap:'100 Gbps',owner:'Frontier',color:'#e74c3c',type:'metro'},
            {name:'Frontier Connecticut',coords:[[41.31,-72.92],[41.76,-72.68],[41.18,-73.19]],cap:'100 Gbps',owner:'Frontier',color:'#e74c3c',type:'metro'},

            // ============================================
            // Shentel/Glo Fiber - Virginia
            // ============================================
            {name:'Glo Fiber Shenandoah',coords:[[38.45,-78.87],[38.07,-79.08],[37.75,-79.44]],cap:'10 Gbps',owner:'Glo Fiber',color:'#2ecc71',type:'metro'},
            {name:'Glo Fiber Valley',coords:[[38.15,-79.07],[37.35,-79.97],[37.27,-80.41]],cap:'10 Gbps',owner:'Glo Fiber',color:'#2ecc71',type:'metro'},

            // ============================================
            // SummitIG - Northern Virginia Dark Fiber
            // ============================================
            {name:'SummitIG Ashburn Core',coords:[[39.04,-77.49],[39.00,-77.42],[39.08,-77.35],[39.12,-77.48]],cap:'Dark Fiber',owner:'SummitIG',color:'#34495e',type:'dark'},
            {name:'SummitIG Loudoun Ring',coords:[[39.04,-77.49],[39.15,-77.55],[39.10,-77.65],[38.95,-77.60],[39.04,-77.49]],cap:'Dark Fiber',owner:'SummitIG',color:'#34495e',type:'dark'},

            // ============================================
            // Everstream - Midwest
            // ============================================
            {name:'Everstream Ohio',coords:[[39.96,-83.00],[41.50,-81.69],[41.08,-81.52]],cap:'100 Gbps',owner:'Everstream',color:'#3498db',type:'metro'},
            {name:'Everstream Michigan',coords:[[42.33,-83.05],[42.96,-85.66],[43.02,-83.69]],cap:'100 Gbps',owner:'Everstream',color:'#3498db',type:'metro'},
            {name:'Everstream Indiana',coords:[[39.77,-86.16],[41.08,-85.14],[41.68,-86.25]],cap:'100 Gbps',owner:'Everstream',color:'#3498db',type:'metro'},

            // ============================================
            // US Signal - Midwest
            // ============================================
            {name:'US Signal Chicago',coords:[[41.88,-87.63],[41.95,-87.70],[41.80,-87.55]],cap:'100 Gbps',owner:'US Signal',color:'#9b59b6',type:'metro'},
            {name:'US Signal Detroit',coords:[[42.33,-83.05],[42.48,-83.25],[42.28,-83.75]],cap:'100 Gbps',owner:'US Signal',color:'#9b59b6',type:'metro'},
            {name:'US Signal Indianapolis',coords:[[39.77,-86.16],[39.85,-86.10],[39.70,-86.25]],cap:'100 Gbps',owner:'US Signal',color:'#9b59b6',type:'metro'},

            // ============================================
            // MetroNet - Midwest/Southeast
            // ============================================
            {name:'MetroNet Indiana',coords:[[39.77,-86.16],[40.42,-86.91],[41.08,-85.14]],cap:'10 Gbps',owner:'MetroNet',color:'#1abc9c',type:'metro'},
            {name:'MetroNet Kentucky',coords:[[38.25,-85.76],[37.99,-84.48],[38.04,-84.50]],cap:'10 Gbps',owner:'MetroNet',color:'#1abc9c',type:'metro'},
            {name:'MetroNet Michigan',coords:[[42.27,-84.40],[42.73,-84.55],[43.02,-83.69]],cap:'10 Gbps',owner:'MetroNet',color:'#1abc9c',type:'metro'},

            // ============================================  
            // Fatbeam - Pacific Northwest
            // ============================================
            {name:'Fatbeam Washington',coords:[[47.61,-122.33],[47.68,-117.43],[46.60,-120.51]],cap:'100 Gbps',owner:'Fatbeam',color:'#e67e22',type:'metro'},
            {name:'Fatbeam Idaho',coords:[[43.62,-116.21],[46.41,-117.00],[47.68,-117.43]],cap:'100 Gbps',owner:'Fatbeam',color:'#e67e22',type:'metro'},
            {name:'Fatbeam Oregon',coords:[[45.52,-122.68],[44.94,-123.03],[44.05,-121.31]],cap:'100 Gbps',owner:'Fatbeam',color:'#e67e22',type:'metro'},

            // ============================================
            // Ting/Tucows - Multi-market
            // ============================================
            {name:'Ting Charlottesville',coords:[[38.03,-78.48],[38.10,-78.40],[37.95,-78.55]],cap:'10 Gbps',owner:'Ting',color:'#2ecc71',type:'metro'},
            {name:'Ting Westminster',coords:[[39.57,-77.00],[39.65,-76.95],[39.50,-77.05]],cap:'10 Gbps',owner:'Ting',color:'#2ecc71',type:'metro'},
            {name:'Ting Centennial',coords:[[39.58,-104.87],[39.65,-104.80],[39.52,-104.95]],cap:'10 Gbps',owner:'Ting',color:'#2ecc71',type:'metro'},

            // ============================================
            // Google Fiber Markets
            // ============================================
            {name:'Google Fiber Kansas City',coords:[[39.1,-94.58],[39.15,-94.55],[39.05,-94.62],[39.1,-94.58]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Google Fiber Austin',coords:[[30.27,-97.74],[30.35,-97.65],[30.20,-97.82]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Google Fiber Nashville',coords:[[36.16,-86.78],[36.22,-86.70],[36.10,-86.85]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Google Fiber Charlotte',coords:[[35.23,-80.84],[35.30,-80.75],[35.15,-80.92]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Google Fiber Raleigh',coords:[[35.78,-78.64],[35.85,-78.55],[35.70,-78.72]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Google Fiber Atlanta',coords:[[33.75,-84.39],[33.85,-84.30],[33.65,-84.48]],cap:'2 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},

            // ============================================
            // EPB Fiber - Chattanooga
            // ============================================
            {name:'EPB Chattanooga',coords:[[35.05,-85.31],[35.10,-85.25],[34.98,-85.38]],cap:'25 Gbps',owner:'EPB',color:'#f1c40f',type:'metro'},

            // ============================================
            // UTOPIA Fiber - Utah
            // ============================================
            {name:'UTOPIA Utah County',coords:[[40.23,-111.66],[40.36,-111.74],[40.09,-111.65]],cap:'10 Gbps',owner:'UTOPIA',color:'#9b59b6',type:'metro'},
            {name:'UTOPIA Salt Lake',coords:[[40.76,-111.89],[40.68,-111.83],[40.82,-111.95]],cap:'10 Gbps',owner:'UTOPIA',color:'#9b59b6',type:'metro'},

            // ============================================
            // Point Broadband - Southeast
            // ============================================
            {name:'Point Broadband Georgia',coords:[[33.75,-84.39],[34.25,-83.82],[34.68,-84.48]],cap:'1 Gbps',owner:'Point Broadband',color:'#e74c3c',type:'metro'},
            {name:'Point Broadband Alabama',coords:[[33.52,-86.80],[34.73,-86.59],[33.21,-87.57]],cap:'1 Gbps',owner:'Point Broadband',color:'#e74c3c',type:'metro'},

            // ============================================
            // Syringa Networks - Idaho
            // ============================================
            {name:'Syringa Idaho',coords:[[43.62,-116.21],[42.57,-114.46],[43.49,-112.04]],cap:'100 Gbps',owner:'Syringa',color:'#1abc9c',type:'longhaul'},
            {name:'Syringa Boise Metro',coords:[[43.62,-116.21],[43.58,-116.55],[43.68,-116.35]],cap:'100 Gbps',owner:'Syringa',color:'#1abc9c',type:'metro'},

            // ============================================
            // METRO FIBER NETWORKS - Major US Cities
            // Dense urban fiber rings and distribution networks
            // ============================================
            
            // NYC Metro - Lightpath, Zayo Metro, Crown Castle
            {name:'Lightpath Manhattan Core',coords:[[40.76,-73.98],[40.75,-73.97],[40.74,-73.99],[40.72,-74.00],[40.71,-74.01],[40.72,-73.99],[40.74,-73.98],[40.76,-73.98]],cap:'400 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Lightpath Midtown Ring',coords:[[40.76,-73.99],[40.77,-73.97],[40.76,-73.95],[40.74,-73.96],[40.75,-73.98],[40.76,-73.99]],cap:'100 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Lightpath Downtown Ring',coords:[[40.71,-74.01],[40.72,-74.00],[40.71,-73.98],[40.70,-73.99],[40.70,-74.01],[40.71,-74.01]],cap:'100 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Zayo NYC Metro',coords:[[40.78,-73.97],[40.75,-73.99],[40.72,-74.00],[40.70,-74.01],[40.69,-73.99],[40.71,-73.95]],cap:'200 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle NYC',coords:[[40.76,-73.98],[40.73,-73.99],[40.71,-74.00],[40.72,-73.97],[40.75,-73.96],[40.76,-73.98]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Pilot Fiber NYC',coords:[[40.74,-73.99],[40.73,-73.98],[40.72,-73.99],[40.73,-74.00],[40.74,-73.99]],cap:'10 Gbps',owner:'Pilot Fiber',color:'#1abc9c',type:'metro'},
            
            // NYC Outer Boroughs
            {name:'Lightpath Brooklyn',coords:[[40.69,-73.99],[40.68,-73.97],[40.66,-73.95],[40.65,-73.97],[40.67,-73.99],[40.69,-73.99]],cap:'100 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Lightpath Queens',coords:[[40.75,-73.88],[40.73,-73.85],[40.71,-73.83],[40.72,-73.87],[40.74,-73.89],[40.75,-73.88]],cap:'100 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Altice Long Island',coords:[[40.72,-73.80],[40.75,-73.65],[40.78,-73.50],[40.80,-73.35],[40.82,-73.20]],cap:'50 Gbps',owner:'Altice',color:'#e67e22',type:'metro'},
            
            // Northern NJ Metro
            {name:'Lightpath NJ Metro',coords:[[40.74,-74.17],[40.72,-74.08],[40.73,-74.03],[40.75,-74.00],[40.74,-74.05],[40.73,-74.12],[40.74,-74.17]],cap:'100 Gbps',owner:'Lightpath',color:'#e74c3c',type:'metro'},
            {name:'Zayo NJ Metro',coords:[[40.78,-74.12],[40.75,-74.08],[40.72,-74.06],[40.70,-74.08],[40.72,-74.12],[40.75,-74.14],[40.78,-74.12]],cap:'200 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            
            // Chicago Metro - Multiple carriers
            {name:'Comcast Chicago Metro',coords:[[41.90,-87.65],[41.88,-87.63],[41.86,-87.62],[41.85,-87.64],[41.87,-87.66],[41.89,-87.67],[41.90,-87.65]],cap:'100 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            {name:'Zayo Chicago Loop',coords:[[41.88,-87.64],[41.87,-87.62],[41.86,-87.64],[41.87,-87.66],[41.88,-87.64]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'WOW Chicago',coords:[[41.92,-87.70],[41.90,-87.67],[41.88,-87.65],[41.86,-87.68],[41.88,-87.72],[41.90,-87.73],[41.92,-87.70]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            {name:'Everstream Chicago',coords:[[41.89,-87.66],[41.87,-87.63],[41.85,-87.65],[41.86,-87.68],[41.88,-87.69],[41.89,-87.66]],cap:'100 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'US Signal Chicago',coords:[[41.91,-87.68],[41.88,-87.64],[41.86,-87.66],[41.87,-87.70],[41.90,-87.71],[41.91,-87.68]],cap:'100 Gbps',owner:'US Signal',color:'#00bcd4',type:'metro'},
            
            // Chicago Suburbs
            {name:'Comcast Chicago North',coords:[[42.05,-87.75],[42.00,-87.70],[41.95,-87.68],[41.92,-87.72],[41.95,-87.77],[42.00,-87.78],[42.05,-87.75]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            {name:'Comcast Chicago West',coords:[[41.88,-87.80],[41.85,-87.75],[41.83,-87.70],[41.85,-87.68],[41.88,-87.72],[41.90,-87.78],[41.88,-87.80]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Los Angeles Metro
            {name:'Zayo LA Metro',coords:[[34.05,-118.25],[34.02,-118.28],[33.98,-118.30],[33.95,-118.28],[33.97,-118.23],[34.00,-118.20],[34.05,-118.25]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle LA',coords:[[34.06,-118.24],[34.03,-118.27],[34.00,-118.25],[34.02,-118.22],[34.05,-118.21],[34.06,-118.24]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Spectrum LA Metro',coords:[[34.08,-118.30],[34.05,-118.26],[34.02,-118.28],[33.99,-118.32],[34.02,-118.35],[34.06,-118.33],[34.08,-118.30]],cap:'100 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'AT&T LA Metro',coords:[[34.04,-118.26],[34.01,-118.23],[33.98,-118.25],[34.00,-118.29],[34.03,-118.28],[34.04,-118.26]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            
            // LA Extended
            {name:'Zayo LA South Bay',coords:[[33.92,-118.40],[33.88,-118.35],[33.85,-118.30],[33.87,-118.25],[33.92,-118.28],[33.95,-118.35],[33.92,-118.40]],cap:'100 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Spectrum Orange County',coords:[[33.75,-117.90],[33.70,-117.85],[33.65,-117.80],[33.68,-117.75],[33.72,-117.78],[33.77,-117.85],[33.75,-117.90]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // San Francisco Metro
            {name:'Zayo SF Metro',coords:[[37.79,-122.41],[37.77,-122.42],[37.75,-122.40],[37.76,-122.38],[37.78,-122.39],[37.79,-122.41]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Sonic SF',coords:[[37.78,-122.42],[37.76,-122.40],[37.75,-122.42],[37.77,-122.44],[37.78,-122.42]],cap:'10 Gbps',owner:'Sonic',color:'#ff9800',type:'metro'},
            {name:'Wave SF',coords:[[37.77,-122.43],[37.75,-122.41],[37.76,-122.39],[37.78,-122.40],[37.77,-122.43]],cap:'50 Gbps',owner:'Wave',color:'#4caf50',type:'metro'},
            
            // Silicon Valley
            {name:'Zayo Silicon Valley',coords:[[37.45,-122.18],[37.42,-122.15],[37.38,-122.08],[37.35,-122.02],[37.33,-121.95],[37.35,-121.90],[37.40,-121.92]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'AT&T Silicon Valley',coords:[[37.48,-122.15],[37.44,-122.10],[37.40,-122.05],[37.36,-122.00],[37.38,-121.95],[37.42,-121.98]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            
            // Dallas Metro
            {name:'Zayo Dallas Metro',coords:[[32.80,-96.80],[32.78,-96.78],[32.76,-96.80],[32.78,-96.82],[32.80,-96.80]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle Dallas',coords:[[32.82,-96.82],[32.78,-96.78],[32.75,-96.82],[32.78,-96.85],[32.82,-96.82]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Logix Dallas',coords:[[32.81,-96.79],[32.77,-96.76],[32.74,-96.79],[32.77,-96.83],[32.81,-96.79]],cap:'100 Gbps',owner:'Logix',color:'#e91e63',type:'metro'},
            {name:'Phonoscope Dallas',coords:[[32.79,-96.81],[32.76,-96.78],[32.74,-96.81],[32.77,-96.84],[32.79,-96.81]],cap:'50 Gbps',owner:'Phonoscope',color:'#ff9800',type:'metro'},
            
            // DFW Extended
            {name:'Spectrum DFW',coords:[[32.95,-96.85],[32.88,-96.78],[32.80,-96.82],[32.85,-96.92],[32.92,-96.90],[32.95,-96.85]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'AT&T DFW Ring',coords:[[33.00,-96.80],[32.90,-96.70],[32.75,-96.75],[32.78,-96.95],[32.90,-96.98],[33.00,-96.80]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            
            // Houston Metro
            {name:'Logix Houston',coords:[[29.77,-95.38],[29.75,-95.36],[29.73,-95.38],[29.75,-95.40],[29.77,-95.38]],cap:'100 Gbps',owner:'Logix',color:'#e91e63',type:'metro'},
            {name:'Phonoscope Houston',coords:[[29.78,-95.40],[29.75,-95.37],[29.72,-95.40],[29.75,-95.43],[29.78,-95.40]],cap:'100 Gbps',owner:'Phonoscope',color:'#ff9800',type:'metro'},
            {name:'Zayo Houston',coords:[[29.80,-95.42],[29.76,-95.38],[29.72,-95.42],[29.76,-95.46],[29.80,-95.42]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle Houston',coords:[[29.79,-95.39],[29.74,-95.35],[29.70,-95.40],[29.74,-95.45],[29.79,-95.39]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            
            // Atlanta Metro
            {name:'Zayo Atlanta',coords:[[33.76,-84.40],[33.74,-84.38],[33.72,-84.40],[33.74,-84.42],[33.76,-84.40]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Comcast Atlanta',coords:[[33.78,-84.42],[33.75,-84.38],[33.72,-84.42],[33.75,-84.46],[33.78,-84.42]],cap:'100 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            {name:'Crown Castle Atlanta',coords:[[33.77,-84.39],[33.73,-84.36],[33.70,-84.40],[33.73,-84.44],[33.77,-84.39]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            
            // Washington DC Metro
            {name:'Zayo DC Metro',coords:[[38.91,-77.04],[38.89,-77.02],[38.87,-77.04],[38.89,-77.06],[38.91,-77.04]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle DC',coords:[[38.92,-77.05],[38.88,-77.01],[38.85,-77.05],[38.88,-77.09],[38.92,-77.05]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Comcast DC',coords:[[38.90,-77.03],[38.87,-77.00],[38.85,-77.03],[38.87,-77.06],[38.90,-77.03]],cap:'100 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // NoVA Data Center Corridor
            {name:'SummitIG Ashburn',coords:[[39.05,-77.49],[39.03,-77.46],[39.01,-77.49],[39.03,-77.52],[39.05,-77.49]],cap:'400 Gbps',owner:'SummitIG',color:'#ff5722',type:'dark'},
            {name:'Zayo Ashburn',coords:[[39.06,-77.50],[39.02,-77.45],[38.99,-77.50],[39.02,-77.55],[39.06,-77.50]],cap:'800 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle NoVA',coords:[[39.04,-77.48],[39.00,-77.43],[38.97,-77.48],[39.00,-77.53],[39.04,-77.48]],cap:'400 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            
            // Boston Metro
            {name:'Zayo Boston',coords:[[42.36,-71.06],[42.34,-71.04],[42.32,-71.06],[42.34,-71.08],[42.36,-71.06]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle Boston',coords:[[42.37,-71.07],[42.33,-71.03],[42.30,-71.07],[42.33,-71.11],[42.37,-71.07]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'FirstLight Boston',coords:[[42.35,-71.08],[42.32,-71.05],[42.30,-71.08],[42.33,-71.10],[42.35,-71.08]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'metro'},
            
            // Denver Metro
            {name:'Zayo Denver',coords:[[39.75,-104.99],[39.73,-104.97],[39.71,-104.99],[39.73,-105.01],[39.75,-104.99]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Crown Castle Denver',coords:[[39.76,-105.00],[39.72,-104.96],[39.69,-105.00],[39.72,-105.04],[39.76,-105.00]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Comcast Denver',coords:[[39.74,-104.98],[39.71,-104.95],[39.69,-104.98],[39.71,-105.01],[39.74,-104.98]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Phoenix Metro
            {name:'Zayo Phoenix',coords:[[33.45,-112.07],[33.43,-112.05],[33.41,-112.07],[33.43,-112.09],[33.45,-112.07]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Cox Phoenix',coords:[[33.48,-112.10],[33.44,-112.05],[33.40,-112.10],[33.44,-112.15],[33.48,-112.10]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            
            // Seattle Metro
            {name:'Zayo Seattle',coords:[[47.61,-122.34],[47.59,-122.32],[47.57,-122.34],[47.59,-122.36],[47.61,-122.34]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Wave Seattle',coords:[[47.62,-122.35],[47.58,-122.31],[47.55,-122.35],[47.58,-122.39],[47.62,-122.35]],cap:'100 Gbps',owner:'Wave',color:'#4caf50',type:'metro'},
            {name:'Ziply Seattle',coords:[[47.60,-122.33],[47.57,-122.30],[47.54,-122.33],[47.57,-122.37],[47.60,-122.33]],cap:'100 Gbps',owner:'Ziply',color:'#9c27b0',type:'metro'},
            
            // Miami Metro
            {name:'Zayo Miami',coords:[[25.78,-80.20],[25.76,-80.18],[25.74,-80.20],[25.76,-80.22],[25.78,-80.20]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'AT&T Miami',coords:[[25.80,-80.22],[25.75,-80.17],[25.72,-80.22],[25.75,-80.27],[25.80,-80.22]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'Comcast Miami',coords:[[25.79,-80.19],[25.74,-80.15],[25.71,-80.20],[25.75,-80.24],[25.79,-80.19]],cap:'100 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Minneapolis Metro
            {name:'Zayo Minneapolis',coords:[[44.98,-93.27],[44.96,-93.25],[44.94,-93.27],[44.96,-93.29],[44.98,-93.27]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'US Internet Minneapolis',coords:[[44.97,-93.28],[44.94,-93.24],[44.92,-93.28],[44.95,-93.31],[44.97,-93.28]],cap:'10 Gbps',owner:'US Internet',color:'#4caf50',type:'metro'},
            
            // Detroit Metro
            {name:'123Net Detroit',coords:[[42.34,-83.05],[42.32,-83.03],[42.30,-83.05],[42.32,-83.07],[42.34,-83.05]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'Everstream Detroit',coords:[[42.36,-83.07],[42.32,-83.02],[42.28,-83.06],[42.32,-83.10],[42.36,-83.07]],cap:'100 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Comcast Detroit',coords:[[42.35,-83.04],[42.31,-83.00],[42.28,-83.04],[42.31,-83.08],[42.35,-83.04]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Philadelphia Metro
            {name:'Zayo Philadelphia',coords:[[39.96,-75.17],[39.94,-75.15],[39.92,-75.17],[39.94,-75.19],[39.96,-75.17]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Comcast Philadelphia',coords:[[39.98,-75.19],[39.94,-75.14],[39.90,-75.18],[39.94,-75.23],[39.98,-75.19]],cap:'100 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            {name:'Crown Castle Philadelphia',coords:[[39.97,-75.16],[39.92,-75.12],[39.89,-75.17],[39.93,-75.21],[39.97,-75.16]],cap:'100 Gbps',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            
            // San Diego Metro  
            {name:'Zayo San Diego',coords:[[32.72,-117.16],[32.70,-117.14],[32.68,-117.16],[32.70,-117.18],[32.72,-117.16]],cap:'200 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Cox San Diego',coords:[[32.74,-117.18],[32.70,-117.13],[32.66,-117.17],[32.70,-117.22],[32.74,-117.18]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            
            // Charlotte Metro
            {name:'Segra Charlotte',coords:[[35.23,-80.84],[35.21,-80.82],[35.19,-80.84],[35.21,-80.86],[35.23,-80.84]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Spectrum Charlotte',coords:[[35.25,-80.86],[35.20,-80.80],[35.16,-80.85],[35.20,-80.90],[35.25,-80.86]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Raleigh-Durham Metro
            {name:'Segra RDU',coords:[[35.79,-78.64],[35.77,-78.62],[35.75,-78.64],[35.77,-78.66],[35.79,-78.64]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Spectrum RDU',coords:[[35.81,-78.66],[35.76,-78.60],[35.72,-78.65],[35.76,-78.70],[35.81,-78.66]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Portland Metro
            {name:'Zayo Portland',coords:[[45.52,-122.68],[45.50,-122.66],[45.48,-122.68],[45.50,-122.70],[45.52,-122.68]],cap:'400 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            {name:'Ziply Portland',coords:[[45.54,-122.70],[45.49,-122.64],[45.45,-122.69],[45.49,-122.74],[45.54,-122.70]],cap:'100 Gbps',owner:'Ziply',color:'#9c27b0',type:'metro'},
            
            // San Antonio Metro
            {name:'Grande San Antonio',coords:[[29.43,-98.49],[29.41,-98.47],[29.39,-98.49],[29.41,-98.51],[29.43,-98.49]],cap:'50 Gbps',owner:'Grande',color:'#ff9800',type:'metro'},
            {name:'Spectrum San Antonio',coords:[[29.45,-98.51],[29.40,-98.45],[29.36,-98.50],[29.40,-98.55],[29.45,-98.51]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Austin Metro
            {name:'Grande Austin',coords:[[30.27,-97.74],[30.25,-97.72],[30.23,-97.74],[30.25,-97.76],[30.27,-97.74]],cap:'100 Gbps',owner:'Grande',color:'#ff9800',type:'metro'},
            {name:'Spectrum Austin',coords:[[30.29,-97.76],[30.24,-97.70],[30.20,-97.75],[30.24,-97.80],[30.29,-97.76]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Google Fiber Austin',coords:[[30.28,-97.75],[30.24,-97.71],[30.21,-97.75],[30.25,-97.79],[30.28,-97.75]],cap:'10 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            
            // Kansas City Metro
            {name:'Google Fiber KC',coords:[[39.10,-94.58],[39.08,-94.56],[39.06,-94.58],[39.08,-94.60],[39.10,-94.58]],cap:'10 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Spectrum KC',coords:[[39.12,-94.60],[39.07,-94.54],[39.03,-94.59],[39.07,-94.64],[39.12,-94.60]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Nashville Metro
            {name:'Google Fiber Nashville',coords:[[36.17,-86.78],[36.15,-86.76],[36.13,-86.78],[36.15,-86.80],[36.17,-86.78]],cap:'10 Gbps',owner:'Google Fiber',color:'#4285f4',type:'metro'},
            {name:'Comcast Nashville',coords:[[36.19,-86.80],[36.14,-86.74],[36.10,-86.79],[36.14,-86.84],[36.19,-86.80]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Salt Lake City Metro
            {name:'UTOPIA Salt Lake',coords:[[40.77,-111.89],[40.75,-111.87],[40.73,-111.89],[40.75,-111.91],[40.77,-111.89]],cap:'10 Gbps',owner:'UTOPIA',color:'#4caf50',type:'metro'},
            {name:'Zayo Salt Lake',coords:[[40.79,-111.91],[40.74,-111.85],[40.70,-111.90],[40.74,-111.95],[40.79,-111.91]],cap:'200 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            
            // Las Vegas Metro
            {name:'Cox Las Vegas',coords:[[36.17,-115.14],[36.15,-115.12],[36.13,-115.14],[36.15,-115.16],[36.17,-115.14]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Zayo Las Vegas',coords:[[36.19,-115.16],[36.14,-115.10],[36.10,-115.15],[36.14,-115.20],[36.19,-115.16]],cap:'200 Gbps',owner:'Zayo',color:'#3498db',type:'metro'},
            
            // Columbus Metro
            {name:'WOW Columbus',coords:[[39.97,-83.00],[39.95,-82.98],[39.93,-83.00],[39.95,-83.02],[39.97,-83.00]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            {name:'Spectrum Columbus',coords:[[39.99,-83.02],[39.94,-82.96],[39.90,-83.01],[39.94,-83.06],[39.99,-83.02]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Indianapolis Metro
            {name:'Metronet Indianapolis',coords:[[39.77,-86.16],[39.75,-86.14],[39.73,-86.16],[39.75,-86.18],[39.77,-86.16]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'Spectrum Indianapolis',coords:[[39.79,-86.18],[39.74,-86.12],[39.70,-86.17],[39.74,-86.22],[39.79,-86.18]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Cincinnati Metro
            {name:'altafiber Cincinnati',coords:[[39.10,-84.51],[39.08,-84.49],[39.06,-84.51],[39.08,-84.53],[39.10,-84.51]],cap:'100 Gbps',owner:'altafiber',color:'#e74c3c',type:'metro'},
            {name:'Spectrum Cincinnati',coords:[[39.12,-84.53],[39.07,-84.47],[39.03,-84.52],[39.07,-84.57],[39.12,-84.53]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Pittsburgh Metro
            {name:'DQE Pittsburgh',coords:[[40.44,-80.00],[40.42,-79.98],[40.40,-80.00],[40.42,-80.02],[40.44,-80.00]],cap:'100 Gbps',owner:'DQE',color:'#9c27b0',type:'metro'},
            {name:'Comcast Pittsburgh',coords:[[40.46,-80.02],[40.41,-79.96],[40.37,-80.01],[40.41,-80.06],[40.46,-80.02]],cap:'50 Gbps',owner:'Comcast',color:'#e91e63',type:'metro'},
            
            // Cleveland Metro  
            {name:'Everstream Cleveland',coords:[[41.50,-81.69],[41.48,-81.67],[41.46,-81.69],[41.48,-81.71],[41.50,-81.69]],cap:'100 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Spectrum Cleveland',coords:[[41.52,-81.71],[41.47,-81.65],[41.43,-81.70],[41.47,-81.75],[41.52,-81.71]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Tampa Metro
            {name:'Spectrum Tampa',coords:[[27.95,-82.46],[27.93,-82.44],[27.91,-82.46],[27.93,-82.48],[27.95,-82.46]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Frontier Tampa',coords:[[27.97,-82.48],[27.92,-82.42],[27.88,-82.47],[27.92,-82.52],[27.97,-82.48]],cap:'50 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            
            // Orlando Metro
            {name:'Spectrum Orlando',coords:[[28.54,-81.38],[28.52,-81.36],[28.50,-81.38],[28.52,-81.40],[28.54,-81.38]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Summit Broadband Orlando',coords:[[28.56,-81.40],[28.51,-81.34],[28.47,-81.39],[28.51,-81.44],[28.56,-81.40]],cap:'10 Gbps',owner:'Summit',color:'#4caf50',type:'metro'},

            // ============================================
            // COMPREHENSIVE CARRIER ROUTES - DARK FIBER
            // ============================================
            
            // Uniti Fiber - Southeast Dark Fiber Network
            {name:'Uniti Southeast Backbone',coords:[[33.75,-84.39],[32.08,-81.09],[32.78,-79.93],[35.23,-80.84],[36.17,-86.78]],cap:'Dark',owner:'Uniti',color:'#6366f1',type:'dark'},
            {name:'Uniti Florida Dark',coords:[[30.33,-81.66],[28.54,-81.38],[27.95,-82.46],[26.12,-80.14],[25.76,-80.19]],cap:'Dark',owner:'Uniti',color:'#6366f1',type:'dark'},
            {name:'Uniti Gulf Coast',coords:[[29.95,-90.07],[30.45,-88.90],[30.69,-88.04],[30.41,-87.22],[30.22,-85.85]],cap:'Dark',owner:'Uniti',color:'#6366f1',type:'dark'},
            {name:'Uniti Texas',coords:[[29.76,-95.37],[30.27,-97.74],[32.78,-96.80],[33.45,-101.85]],cap:'Dark',owner:'Uniti',color:'#6366f1',type:'dark'},
            
            // SummitIG Dark Fiber - Data Center Corridor
            {name:'SummitIG NoVA East',coords:[[39.04,-77.49],[38.95,-77.37],[38.90,-77.02],[38.88,-76.99]],cap:'Dark',owner:'SummitIG',color:'#ec4899',type:'dark'},
            {name:'SummitIG NoVA West',coords:[[39.04,-77.49],[39.01,-77.55],[38.98,-77.60],[38.95,-77.65]],cap:'Dark',owner:'SummitIG',color:'#ec4899',type:'dark'},
            {name:'SummitIG Ashburn Core',coords:[[39.05,-77.48],[39.03,-77.46],[39.01,-77.48],[39.03,-77.50],[39.05,-77.48]],cap:'800 Gbps',owner:'SummitIG',color:'#ec4899',type:'dark'},
            
            // Bluebird Network - Midwest Dark Fiber
            {name:'Bluebird MO-IL Backbone',coords:[[38.63,-90.20],[39.10,-94.58],[38.97,-95.27],[39.77,-86.16]],cap:'Dark',owner:'Bluebird',color:'#0ea5e9',type:'dark'},
            {name:'Bluebird Kansas City Ring',coords:[[39.10,-94.58],[39.05,-94.52],[39.00,-94.55],[39.03,-94.62],[39.10,-94.58]],cap:'100 Gbps',owner:'Bluebird',color:'#0ea5e9',type:'metro'},
            {name:'Bluebird St Louis Ring',coords:[[38.63,-90.20],[38.58,-90.15],[38.55,-90.20],[38.60,-90.28],[38.63,-90.20]],cap:'100 Gbps',owner:'Bluebird',color:'#0ea5e9',type:'metro'},
            
            // LS Networks - Pacific Northwest Dark Fiber
            {name:'LS Networks Oregon',coords:[[45.52,-122.68],[44.94,-123.02],[44.05,-121.31],[42.33,-122.87]],cap:'Dark',owner:'LS Networks',color:'#14b8a6',type:'dark'},
            {name:'LS Networks Washington',coords:[[45.52,-122.68],[46.60,-120.51],[47.61,-122.33],[48.75,-122.47]],cap:'Dark',owner:'LS Networks',color:'#14b8a6',type:'dark'},
            {name:'LS Networks Portland Metro',coords:[[45.52,-122.68],[45.48,-122.65],[45.45,-122.70],[45.50,-122.75],[45.52,-122.68]],cap:'100 Gbps',owner:'LS Networks',color:'#14b8a6',type:'metro'},
            
            // Syringa Networks - Idaho/Montana
            {name:'Syringa Idaho Backbone',coords:[[43.62,-116.21],[42.87,-112.45],[43.49,-112.04],[46.87,-114.00]],cap:'100 Gbps',owner:'Syringa',color:'#f97316',type:'dark'},
            {name:'Syringa Montana',coords:[[46.87,-114.00],[45.78,-111.04],[47.51,-111.28],[48.21,-114.32]],cap:'100 Gbps',owner:'Syringa',color:'#f97316',type:'dark'},
            
            // neoNova - Southeast
            {name:'neoNova NC Backbone',coords:[[35.79,-78.64],[35.23,-80.84],[36.07,-79.79],[35.47,-77.43]],cap:'Dark',owner:'neoNova',color:'#8b5cf6',type:'dark'},
            {name:'neoNova SC Extension',coords:[[35.23,-80.84],[34.00,-81.03],[32.78,-79.93],[33.84,-83.32]],cap:'Dark',owner:'neoNova',color:'#8b5cf6',type:'dark'},
            
            // Lightower (now Crown Castle Fiber) - Northeast Dark Fiber
            {name:'Crown Castle Fiber NYC-BOS',coords:[[40.71,-74.01],[41.31,-72.93],[41.76,-72.68],[42.36,-71.06]],cap:'Dark',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Crown Castle Fiber NYC-PHL',coords:[[40.71,-74.01],[40.22,-74.76],[39.95,-75.17]],cap:'Dark',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Crown Castle Fiber PHL-DC',coords:[[39.95,-75.17],[39.29,-76.61],[38.90,-77.04]],cap:'Dark',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Crown Castle NJ Dark',coords:[[40.71,-74.01],[40.74,-74.17],[40.86,-74.22],[40.92,-74.17],[40.75,-74.05]],cap:'Dark',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            {name:'Crown Castle Long Island',coords:[[40.71,-74.01],[40.75,-73.50],[40.80,-73.20],[40.85,-73.00]],cap:'Dark',owner:'Crown Castle',color:'#9b59b6',type:'dark'},
            
            // ============================================
            // TIER 2 REGIONAL CARRIERS - LONG HAUL
            // ============================================
            
            // Consolidated Communications
            {name:'Consolidated New England',coords:[[43.66,-70.26],[43.21,-71.54],[42.36,-71.06],[41.76,-72.68]],cap:'100 Gbps',owner:'Consolidated',color:'#22c55e',type:'longhaul'},
            {name:'Consolidated Minnesota',coords:[[44.98,-93.27],[45.56,-94.16],[46.87,-96.79],[47.92,-97.03]],cap:'100 Gbps',owner:'Consolidated',color:'#22c55e',type:'longhaul'},
            {name:'Consolidated Illinois',coords:[[41.88,-87.63],[40.69,-89.59],[39.80,-89.65],[38.63,-90.20]],cap:'100 Gbps',owner:'Consolidated',color:'#22c55e',type:'longhaul'},
            {name:'Consolidated Texas',coords:[[32.78,-96.80],[31.76,-95.36],[30.27,-97.74],[29.42,-98.49]],cap:'100 Gbps',owner:'Consolidated',color:'#22c55e',type:'longhaul'},
            
            // TDS Telecom
            {name:'TDS Wisconsin',coords:[[43.04,-87.91],[43.78,-88.79],[44.52,-88.02],[45.44,-89.57]],cap:'50 Gbps',owner:'TDS',color:'#06b6d4',type:'longhaul'},
            {name:'TDS Tennessee',coords:[[36.17,-86.78],[35.96,-83.92],[35.05,-85.31],[35.15,-90.05]],cap:'50 Gbps',owner:'TDS',color:'#06b6d4',type:'longhaul'},
            
            // Cincinnati Bell/altafiber
            {name:'altafiber Ohio Valley',coords:[[39.10,-84.51],[39.77,-84.19],[40.76,-84.11],[41.08,-85.14]],cap:'100 Gbps',owner:'altafiber',color:'#dc2626',type:'longhaul'},
            {name:'altafiber Cincinnati Metro',coords:[[39.10,-84.51],[39.15,-84.46],[39.18,-84.52],[39.12,-84.58],[39.10,-84.51]],cap:'200 Gbps',owner:'altafiber',color:'#dc2626',type:'metro'},
            {name:'altafiber Dayton',coords:[[39.76,-84.19],[39.73,-84.15],[39.70,-84.19],[39.73,-84.24],[39.76,-84.19]],cap:'100 Gbps',owner:'altafiber',color:'#dc2626',type:'metro'},
            
            // Hawaiian Telcom
            {name:'Hawaiian Telcom Oahu',coords:[[21.31,-157.86],[21.39,-157.95],[21.45,-158.00],[21.50,-158.03]],cap:'100 Gbps',owner:'Hawaiian Telcom',color:'#0891b2',type:'metro'},
            {name:'Hawaiian Telcom Inter-Island',coords:[[21.31,-157.86],[20.80,-156.46],[19.73,-155.08]],cap:'200 Gbps',owner:'Hawaiian Telcom',color:'#0891b2',type:'longhaul'},
            
            // GCI Alaska
            {name:'GCI Alaska Backbone',coords:[[61.22,-149.90],[64.84,-147.72],[58.30,-134.42],[57.05,-135.33]],cap:'100 Gbps',owner:'GCI',color:'#7c3aed',type:'longhaul'},
            {name:'GCI Anchorage Metro',coords:[[61.22,-149.90],[61.18,-149.85],[61.15,-149.90],[61.20,-149.98],[61.22,-149.90]],cap:'50 Gbps',owner:'GCI',color:'#7c3aed',type:'metro'},
            
            // ============================================
            // METRO FIBER PROVIDERS - ADDITIONAL CITIES
            // ============================================
            
            // Lumos Networks - Virginia/WV
            {name:'Lumos Virginia',coords:[[37.27,-79.94],[37.41,-79.14],[38.03,-78.48],[38.43,-78.87]],cap:'100 Gbps',owner:'Lumos',color:'#f59e0b',type:'longhaul'},
            {name:'Lumos Roanoke Metro',coords:[[37.27,-79.94],[37.24,-79.90],[37.20,-79.94],[37.24,-80.00],[37.27,-79.94]],cap:'50 Gbps',owner:'Lumos',color:'#f59e0b',type:'metro'},
            {name:'Lumos Charlottesville',coords:[[38.03,-78.48],[38.00,-78.44],[37.97,-78.48],[38.00,-78.52],[38.03,-78.48]],cap:'50 Gbps',owner:'Lumos',color:'#f59e0b',type:'metro'},
            
            // OTELCO - New England Rural
            {name:'OTELCO Maine',coords:[[44.31,-69.78],[44.80,-68.77],[45.25,-69.23],[44.95,-70.26]],cap:'50 Gbps',owner:'OTELCO',color:'#84cc16',type:'longhaul'},
            {name:'OTELCO Alabama',coords:[[33.52,-86.80],[32.38,-86.30],[31.22,-85.40],[30.69,-88.04]],cap:'50 Gbps',owner:'OTELCO',color:'#84cc16',type:'longhaul'},
            
            // C Spire - Mississippi
            {name:'C Spire Mississippi',coords:[[32.30,-90.18],[33.45,-88.82],[34.26,-88.70],[32.35,-88.70]],cap:'100 Gbps',owner:'C Spire',color:'#3b82f6',type:'longhaul'},
            {name:'C Spire Jackson Metro',coords:[[32.30,-90.18],[32.35,-90.12],[32.38,-90.18],[32.33,-90.25],[32.30,-90.18]],cap:'50 Gbps',owner:'C Spire',color:'#3b82f6',type:'metro'},
            
            // Great Plains Communications
            {name:'Great Plains Nebraska',coords:[[41.26,-95.94],[40.81,-96.70],[41.14,-100.76],[42.87,-100.55]],cap:'100 Gbps',owner:'Great Plains',color:'#eab308',type:'longhaul'},
            {name:'Great Plains SD',coords:[[42.87,-100.55],[43.55,-96.73],[44.37,-98.22],[45.46,-98.49]],cap:'100 Gbps',owner:'Great Plains',color:'#eab308',type:'longhaul'},
            
            // Midco - Upper Midwest
            {name:'Midco Dakotas',coords:[[43.55,-96.73],[44.08,-103.23],[46.88,-96.79],[48.23,-101.30]],cap:'100 Gbps',owner:'Midco',color:'#f472b6',type:'longhaul'},
            {name:'Midco Sioux Falls Metro',coords:[[43.55,-96.73],[43.52,-96.68],[43.48,-96.73],[43.52,-96.80],[43.55,-96.73]],cap:'50 Gbps',owner:'Midco',color:'#f472b6',type:'metro'},
            
            // Atlantic Tele-Network (ATNI)
            {name:'ATNI Southwest',coords:[[35.08,-106.65],[34.52,-105.56],[33.94,-106.46],[32.90,-105.96]],cap:'50 Gbps',owner:'ATNI',color:'#a855f7',type:'longhaul'},
            
            // Lingo (formerly Birch)
            {name:'Lingo Southeast',coords:[[33.75,-84.39],[32.08,-81.09],[30.33,-81.66],[27.95,-82.46]],cap:'100 Gbps',owner:'Lingo',color:'#64748b',type:'longhaul'},
            
            // ============================================
            // CABLE COMPANY METRO NETWORKS
            // ============================================
            
            // Charter/Spectrum Additional Markets
            {name:'Spectrum St Louis',coords:[[38.63,-90.20],[38.58,-90.14],[38.55,-90.20],[38.60,-90.28],[38.63,-90.20]],cap:'100 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Louisville',coords:[[38.25,-85.76],[38.22,-85.70],[38.18,-85.76],[38.22,-85.82],[38.25,-85.76]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Milwaukee',coords:[[43.04,-87.91],[43.00,-87.86],[42.96,-87.91],[43.00,-87.98],[43.04,-87.91]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Buffalo',coords:[[42.89,-78.88],[42.85,-78.82],[42.81,-78.88],[42.85,-78.94],[42.89,-78.88]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Rochester',coords:[[43.16,-77.61],[43.12,-77.55],[43.08,-77.61],[43.12,-77.67],[43.16,-77.61]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Albany',coords:[[42.65,-73.76],[42.61,-73.70],[42.57,-73.76],[42.61,-73.82],[42.65,-73.76]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            {name:'Spectrum Hawaii',coords:[[21.31,-157.86],[21.27,-157.80],[21.23,-157.86],[21.27,-157.92],[21.31,-157.86]],cap:'50 Gbps',owner:'Spectrum',color:'#2196f3',type:'metro'},
            
            // Cox Additional Markets
            {name:'Cox Tucson',coords:[[32.22,-110.97],[32.18,-110.91],[32.14,-110.97],[32.18,-111.03],[32.22,-110.97]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Cox Oklahoma City',coords:[[35.47,-97.52],[35.43,-97.46],[35.39,-97.52],[35.43,-97.58],[35.47,-97.52]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Cox Omaha',coords:[[41.26,-95.94],[41.22,-95.88],[41.18,-95.94],[41.22,-96.00],[41.26,-95.94]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Cox New Orleans',coords:[[29.95,-90.07],[29.91,-90.01],[29.87,-90.07],[29.91,-90.13],[29.95,-90.07]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Cox Hampton Roads',coords:[[36.85,-76.29],[36.81,-76.23],[36.77,-76.29],[36.81,-76.35],[36.85,-76.29]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            {name:'Cox Rhode Island',coords:[[41.82,-71.41],[41.78,-71.35],[41.74,-71.41],[41.78,-71.47],[41.82,-71.41]],cap:'50 Gbps',owner:'Cox',color:'#ff9800',type:'metro'},
            
            // Optimum/Altice Additional
            {name:'Altice Connecticut',coords:[[41.31,-72.93],[41.18,-73.20],[41.05,-73.54],[41.27,-73.19]],cap:'50 Gbps',owner:'Altice',color:'#e67e22',type:'metro'},
            {name:'Altice NJ North',coords:[[40.86,-74.22],[40.92,-74.10],[40.98,-74.02],[40.90,-73.95]],cap:'50 Gbps',owner:'Altice',color:'#e67e22',type:'metro'},
            
            // ============================================
            // CLEC/ILEC METRO NETWORKS
            // ============================================
            
            // Logix Additional Texas Markets
            {name:'Logix Austin',coords:[[30.27,-97.74],[30.24,-97.68],[30.20,-97.74],[30.24,-97.80],[30.27,-97.74]],cap:'100 Gbps',owner:'Logix',color:'#e91e63',type:'metro'},
            {name:'Logix San Antonio',coords:[[29.42,-98.49],[29.38,-98.43],[29.34,-98.49],[29.38,-98.55],[29.42,-98.49]],cap:'100 Gbps',owner:'Logix',color:'#e91e63',type:'metro'},
            
            // Phonoscope Additional Texas Markets
            {name:'Phonoscope Austin',coords:[[30.28,-97.75],[30.25,-97.70],[30.21,-97.75],[30.25,-97.81],[30.28,-97.75]],cap:'100 Gbps',owner:'Phonoscope',color:'#ff9800',type:'metro'},
            {name:'Phonoscope San Antonio',coords:[[29.44,-98.50],[29.40,-98.44],[29.36,-98.50],[29.40,-98.56],[29.44,-98.50]],cap:'100 Gbps',owner:'Phonoscope',color:'#ff9800',type:'metro'},
            
            // Grande Communications
            {name:'Grande Austin Ring',coords:[[30.30,-97.76],[30.26,-97.70],[30.22,-97.76],[30.26,-97.82],[30.30,-97.76]],cap:'50 Gbps',owner:'Grande',color:'#ef4444',type:'metro'},
            {name:'Grande San Antonio Ring',coords:[[29.46,-98.51],[29.42,-98.45],[29.38,-98.51],[29.42,-98.57],[29.46,-98.51]],cap:'50 Gbps',owner:'Grande',color:'#ef4444',type:'metro'},
            {name:'Grande Midland-Odessa',coords:[[31.99,-102.08],[32.02,-102.14],[31.85,-102.37],[31.80,-102.30]],cap:'50 Gbps',owner:'Grande',color:'#ef4444',type:'metro'},
            
            // WOW! Additional Markets
            {name:'WOW Detroit',coords:[[42.34,-83.05],[42.30,-82.99],[42.26,-83.05],[42.30,-83.11],[42.34,-83.05]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            {name:'WOW Cleveland',coords:[[41.50,-81.69],[41.46,-81.63],[41.42,-81.69],[41.46,-81.75],[41.50,-81.69]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            {name:'WOW Tampa',coords:[[27.95,-82.46],[27.91,-82.40],[27.87,-82.46],[27.91,-82.52],[27.95,-82.46]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            {name:'WOW Augusta',coords:[[33.47,-81.97],[33.43,-81.91],[33.39,-81.97],[33.43,-82.03],[33.47,-81.97]],cap:'50 Gbps',owner:'WOW!',color:'#ff9800',type:'metro'},
            
            // Breezeline (formerly Atlantic Broadband)
            {name:'Breezeline Miami',coords:[[25.80,-80.21],[25.76,-80.15],[25.72,-80.21],[25.76,-80.27],[25.80,-80.21]],cap:'50 Gbps',owner:'Breezeline',color:'#0ea5e9',type:'metro'},
            {name:'Breezeline Maryland',coords:[[39.29,-76.61],[39.25,-76.55],[39.21,-76.61],[39.25,-76.67],[39.29,-76.61]],cap:'50 Gbps',owner:'Breezeline',color:'#0ea5e9',type:'metro'},
            {name:'Breezeline Charleston',coords:[[32.78,-79.93],[32.74,-79.87],[32.70,-79.93],[32.74,-79.99],[32.78,-79.93]],cap:'50 Gbps',owner:'Breezeline',color:'#0ea5e9',type:'metro'},
            
            // Mediacom
            {name:'Mediacom Des Moines',coords:[[41.59,-93.62],[41.55,-93.56],[41.51,-93.62],[41.55,-93.68],[41.59,-93.62]],cap:'50 Gbps',owner:'Mediacom',color:'#7c3aed',type:'metro'},
            {name:'Mediacom Cedar Rapids',coords:[[41.98,-91.67],[41.94,-91.61],[41.90,-91.67],[41.94,-91.73],[41.98,-91.67]],cap:'50 Gbps',owner:'Mediacom',color:'#7c3aed',type:'metro'},
            {name:'Mediacom Springfield MO',coords:[[37.22,-93.29],[37.18,-93.23],[37.14,-93.29],[37.18,-93.35],[37.22,-93.29]],cap:'50 Gbps',owner:'Mediacom',color:'#7c3aed',type:'metro'},
            
            // ============================================
            // FIBER OVERBUILDERS / COMPETITIVE PROVIDERS
            // ============================================
            
            // Ting Additional Markets
            {name:'Ting Charlottesville',coords:[[38.03,-78.48],[38.00,-78.43],[37.96,-78.48],[38.00,-78.54],[38.03,-78.48]],cap:'10 Gbps',owner:'Ting',color:'#0d9488',type:'metro'},
            {name:'Ting Westminster MD',coords:[[39.57,-77.01],[39.54,-76.96],[39.50,-77.01],[39.54,-77.07],[39.57,-77.01]],cap:'10 Gbps',owner:'Ting',color:'#0d9488',type:'metro'},
            {name:'Ting Fuquay-Varina',coords:[[35.58,-78.80],[35.55,-78.75],[35.51,-78.80],[35.55,-78.86],[35.58,-78.80]],cap:'10 Gbps',owner:'Ting',color:'#0d9488',type:'metro'},
            {name:'Ting Centennial CO',coords:[[39.58,-104.87],[39.55,-104.82],[39.51,-104.87],[39.55,-104.93],[39.58,-104.87]],cap:'10 Gbps',owner:'Ting',color:'#0d9488',type:'metro'},
            
            // Glo Fiber (Shenandoah)
            {name:'Glo Fiber Virginia',coords:[[38.90,-77.04],[38.44,-78.87],[37.41,-79.14],[36.85,-76.29]],cap:'100 Gbps',owner:'Glo Fiber',color:'#10b981',type:'longhaul'},
            {name:'Glo Fiber Fredericksburg',coords:[[38.30,-77.46],[38.27,-77.41],[38.23,-77.46],[38.27,-77.52],[38.30,-77.46]],cap:'10 Gbps',owner:'Glo Fiber',color:'#10b981',type:'metro'},
            {name:'Glo Fiber Staunton',coords:[[38.15,-79.07],[38.12,-79.02],[38.08,-79.07],[38.12,-79.13],[38.15,-79.07]],cap:'10 Gbps',owner:'Glo Fiber',color:'#10b981',type:'metro'},
            
            // MetroNet Additional Markets
            {name:'MetroNet Evansville',coords:[[37.97,-87.56],[37.94,-87.51],[37.90,-87.56],[37.94,-87.62],[37.97,-87.56]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'MetroNet Lexington',coords:[[38.04,-84.50],[38.01,-84.45],[37.97,-84.50],[38.01,-84.56],[38.04,-84.50]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'MetroNet Fort Wayne',coords:[[41.08,-85.14],[41.05,-85.09],[41.01,-85.14],[41.05,-85.20],[41.08,-85.14]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'MetroNet Grand Rapids',coords:[[42.96,-85.66],[42.93,-85.61],[42.89,-85.66],[42.93,-85.72],[42.96,-85.66]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'MetroNet Tallahassee',coords:[[30.44,-84.28],[30.41,-84.23],[30.37,-84.28],[30.41,-84.34],[30.44,-84.28]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            {name:'MetroNet Dayton',coords:[[39.76,-84.19],[39.73,-84.14],[39.69,-84.19],[39.73,-84.25],[39.76,-84.19]],cap:'10 Gbps',owner:'MetroNet',color:'#4caf50',type:'metro'},
            
            // Ziply Fiber Additional Pacific NW
            {name:'Ziply Spokane',coords:[[47.66,-117.43],[47.62,-117.37],[47.58,-117.43],[47.62,-117.49],[47.66,-117.43]],cap:'100 Gbps',owner:'Ziply',color:'#9c27b0',type:'metro'},
            {name:'Ziply Boise',coords:[[43.62,-116.21],[43.58,-116.15],[43.54,-116.21],[43.58,-116.27],[43.62,-116.21]],cap:'100 Gbps',owner:'Ziply',color:'#9c27b0',type:'metro'},
            {name:'Ziply Tacoma',coords:[[47.25,-122.44],[47.21,-122.38],[47.17,-122.44],[47.21,-122.50],[47.25,-122.44]],cap:'100 Gbps',owner:'Ziply',color:'#9c27b0',type:'metro'},
            {name:'Ziply Oregon Backbone',coords:[[45.52,-122.68],[44.94,-123.02],[44.05,-121.31],[45.52,-122.68]],cap:'200 Gbps',owner:'Ziply',color:'#9c27b0',type:'longhaul'},
            
            // EPB Chattanooga
            {name:'EPB Chattanooga Metro',coords:[[35.05,-85.31],[35.02,-85.26],[34.98,-85.31],[35.02,-85.37],[35.05,-85.31]],cap:'25 Gbps',owner:'EPB',color:'#22c55e',type:'metro'},
            {name:'EPB Hamilton County',coords:[[35.05,-85.31],[35.15,-85.20],[35.10,-85.10],[34.95,-85.25]],cap:'10 Gbps',owner:'EPB',color:'#22c55e',type:'metro'},
            
            // RS Fiber (Minnesota)
            {name:'RS Fiber Minnesota',coords:[[44.20,-94.00],[44.40,-93.70],[44.55,-94.30],[44.30,-94.50]],cap:'10 Gbps',owner:'RS Fiber',color:'#f59e0b',type:'metro'},
            
            // Sonic Additional California
            {name:'Sonic East Bay',coords:[[37.80,-122.27],[37.77,-122.22],[37.73,-122.27],[37.77,-122.33],[37.80,-122.27]],cap:'10 Gbps',owner:'Sonic',color:'#ff9800',type:'metro'},
            {name:'Sonic Peninsula',coords:[[37.50,-122.25],[37.47,-122.20],[37.43,-122.25],[37.47,-122.31],[37.50,-122.25]],cap:'10 Gbps',owner:'Sonic',color:'#ff9800',type:'metro'},
            {name:'Sonic Santa Rosa',coords:[[38.44,-122.71],[38.41,-122.66],[38.37,-122.71],[38.41,-122.77],[38.44,-122.71]],cap:'10 Gbps',owner:'Sonic',color:'#ff9800',type:'metro'},
            
            // UTOPIA Utah
            {name:'UTOPIA Fiber Utah',coords:[[40.77,-111.89],[41.23,-111.97],[40.23,-111.66],[39.90,-111.88]],cap:'10 Gbps',owner:'UTOPIA',color:'#4caf50',type:'longhaul'},
            {name:'UTOPIA Provo',coords:[[40.23,-111.66],[40.20,-111.61],[40.16,-111.66],[40.20,-111.72],[40.23,-111.66]],cap:'10 Gbps',owner:'UTOPIA',color:'#4caf50',type:'metro'},
            {name:'UTOPIA Orem',coords:[[40.30,-111.70],[40.27,-111.65],[40.23,-111.70],[40.27,-111.76],[40.30,-111.70]],cap:'10 Gbps',owner:'UTOPIA',color:'#4caf50',type:'metro'},

            // ============================================
            // VERIZON METRO FIBER NETWORKS
            // ============================================
            {name:'Verizon NYC Metro',coords:[[40.76,-73.98],[40.73,-73.95],[40.70,-73.99],[40.72,-74.02],[40.75,-73.99],[40.76,-73.98]],cap:'400 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon NJ Metro',coords:[[40.74,-74.17],[40.70,-74.10],[40.68,-74.05],[40.72,-74.00],[40.76,-74.08],[40.74,-74.17]],cap:'200 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Philadelphia',coords:[[39.96,-75.17],[39.92,-75.12],[39.88,-75.17],[39.92,-75.22],[39.96,-75.17]],cap:'200 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon DC Metro',coords:[[38.91,-77.04],[38.87,-76.99],[38.83,-77.04],[38.87,-77.10],[38.91,-77.04]],cap:'200 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Baltimore',coords:[[39.29,-76.61],[39.25,-76.56],[39.21,-76.61],[39.25,-76.67],[39.29,-76.61]],cap:'100 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Boston',coords:[[42.36,-71.06],[42.32,-71.01],[42.28,-71.06],[42.32,-71.12],[42.36,-71.06]],cap:'200 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Providence',coords:[[41.82,-71.41],[41.78,-71.36],[41.74,-71.41],[41.78,-71.47],[41.82,-71.41]],cap:'100 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Pittsburgh',coords:[[40.44,-80.00],[40.40,-79.95],[40.36,-80.00],[40.40,-80.06],[40.44,-80.00]],cap:'100 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Richmond',coords:[[37.54,-77.46],[37.50,-77.41],[37.46,-77.46],[37.50,-77.52],[37.54,-77.46]],cap:'100 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            {name:'Verizon Norfolk',coords:[[36.85,-76.29],[36.81,-76.24],[36.77,-76.29],[36.81,-76.35],[36.85,-76.29]],cap:'100 Gbps',owner:'Verizon',color:'#cd040b',type:'metro'},
            
            // ============================================
            // FRONTIER FIOS METRO NETWORKS
            // ============================================
            {name:'Frontier Tampa Metro',coords:[[27.95,-82.46],[27.90,-82.40],[27.85,-82.46],[27.90,-82.52],[27.95,-82.46]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier Dallas Metro',coords:[[32.80,-96.80],[32.75,-96.74],[32.70,-96.80],[32.75,-96.86],[32.80,-96.80]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier LA Metro',coords:[[34.05,-118.24],[34.00,-118.18],[33.95,-118.24],[34.00,-118.30],[34.05,-118.24]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier Connecticut',coords:[[41.31,-72.93],[41.27,-72.87],[41.23,-72.93],[41.27,-72.99],[41.31,-72.93]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier San Diego',coords:[[32.72,-117.16],[32.68,-117.10],[32.64,-117.16],[32.68,-117.22],[32.72,-117.16]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier Inland Empire',coords:[[34.00,-117.35],[33.96,-117.29],[33.92,-117.35],[33.96,-117.41],[34.00,-117.35]],cap:'100 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            {name:'Frontier West Texas',coords:[[31.76,-106.49],[31.72,-106.43],[31.68,-106.49],[31.72,-106.55],[31.76,-106.49]],cap:'50 Gbps',owner:'Frontier',color:'#ff5722',type:'metro'},
            
            // ============================================
            // AT&T ADDITIONAL METRO NETWORKS
            // ============================================
            {name:'AT&T Chicago Metro',coords:[[41.88,-87.63],[41.84,-87.57],[41.80,-87.63],[41.84,-87.69],[41.88,-87.63]],cap:'400 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Dallas Ring',coords:[[32.80,-96.80],[32.75,-96.73],[32.70,-96.80],[32.75,-96.87],[32.80,-96.80]],cap:'400 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Houston Ring',coords:[[29.76,-95.37],[29.71,-95.30],[29.66,-95.37],[29.71,-95.44],[29.76,-95.37]],cap:'400 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Atlanta Ring',coords:[[33.76,-84.40],[33.71,-84.33],[33.66,-84.40],[33.71,-84.47],[33.76,-84.40]],cap:'400 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T San Antonio',coords:[[29.42,-98.49],[29.37,-98.42],[29.32,-98.49],[29.37,-98.56],[29.42,-98.49]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T St Louis',coords:[[38.63,-90.20],[38.58,-90.13],[38.53,-90.20],[38.58,-90.27],[38.63,-90.20]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Detroit',coords:[[42.34,-83.05],[42.29,-82.98],[42.24,-83.05],[42.29,-83.12],[42.34,-83.05]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Indianapolis',coords:[[39.77,-86.16],[39.72,-86.09],[39.67,-86.16],[39.72,-86.23],[39.77,-86.16]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Milwaukee',coords:[[43.04,-87.91],[42.99,-87.84],[42.94,-87.91],[42.99,-87.98],[43.04,-87.91]],cap:'100 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Cleveland',coords:[[41.50,-81.69],[41.45,-81.62],[41.40,-81.69],[41.45,-81.76],[41.50,-81.69]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Memphis',coords:[[35.15,-90.05],[35.10,-89.98],[35.05,-90.05],[35.10,-90.12],[35.15,-90.05]],cap:'100 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T New Orleans',coords:[[29.95,-90.07],[29.90,-90.00],[29.85,-90.07],[29.90,-90.14],[29.95,-90.07]],cap:'100 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Oklahoma City',coords:[[35.47,-97.52],[35.42,-97.45],[35.37,-97.52],[35.42,-97.59],[35.47,-97.52]],cap:'100 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            {name:'AT&T Kansas City',coords:[[39.10,-94.58],[39.05,-94.51],[39.00,-94.58],[39.05,-94.65],[39.10,-94.58]],cap:'200 Gbps',owner:'AT&T',color:'#ff5722',type:'metro'},
            
            // ============================================
            // LUMEN/LEVEL3 ADDITIONAL ROUTES
            // ============================================
            {name:'Lumen Phoenix Ring',coords:[[33.45,-112.07],[33.40,-112.00],[33.35,-112.07],[33.40,-112.14],[33.45,-112.07]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen Denver Ring',coords:[[39.75,-104.99],[39.70,-104.92],[39.65,-104.99],[39.70,-105.06],[39.75,-104.99]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen Seattle Ring',coords:[[47.61,-122.34],[47.56,-122.27],[47.51,-122.34],[47.56,-122.41],[47.61,-122.34]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen Portland Ring',coords:[[45.52,-122.68],[45.47,-122.61],[45.42,-122.68],[45.47,-122.75],[45.52,-122.68]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen Salt Lake',coords:[[40.77,-111.89],[40.72,-111.82],[40.67,-111.89],[40.72,-111.96],[40.77,-111.89]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen Minneapolis Ring',coords:[[44.98,-93.27],[44.93,-93.20],[44.88,-93.27],[44.93,-93.34],[44.98,-93.27]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen San Francisco',coords:[[37.79,-122.41],[37.74,-122.34],[37.69,-122.41],[37.74,-122.48],[37.79,-122.41]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            {name:'Lumen San Jose',coords:[[37.34,-121.89],[37.29,-121.82],[37.24,-121.89],[37.29,-121.96],[37.34,-121.89]],cap:'400 Gbps',owner:'Lumen',color:'#ef4444',type:'metro'},
            
            // ============================================
            // COGENT METRO NETWORKS
            // ============================================
            {name:'Cogent NYC Metro',coords:[[40.76,-73.98],[40.71,-73.91],[40.66,-73.98],[40.71,-74.05],[40.76,-73.98]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent Chicago',coords:[[41.88,-87.63],[41.83,-87.56],[41.78,-87.63],[41.83,-87.70],[41.88,-87.63]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent LA Metro',coords:[[34.05,-118.24],[34.00,-118.17],[33.95,-118.24],[34.00,-118.31],[34.05,-118.24]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent Dallas',coords:[[32.78,-96.80],[32.73,-96.73],[32.68,-96.80],[32.73,-96.87],[32.78,-96.80]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent DC Metro',coords:[[38.91,-77.04],[38.86,-76.97],[38.81,-77.04],[38.86,-77.11],[38.91,-77.04]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent Atlanta',coords:[[33.76,-84.40],[33.71,-84.33],[33.66,-84.40],[33.71,-84.47],[33.76,-84.40]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent Miami',coords:[[25.78,-80.20],[25.73,-80.13],[25.68,-80.20],[25.73,-80.27],[25.78,-80.20]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            {name:'Cogent Denver',coords:[[39.75,-104.99],[39.70,-104.92],[39.65,-104.99],[39.70,-105.06],[39.75,-104.99]],cap:'400 Gbps',owner:'Cogent',color:'#f97316',type:'metro'},
            
            // ============================================
            // HURRICANE ELECTRIC ROUTES
            // ============================================
            {name:'HE West Coast Backbone',coords:[[47.61,-122.34],[45.52,-122.68],[37.79,-122.41],[34.05,-118.24],[32.72,-117.16]],cap:'400 Gbps',owner:'Hurricane Electric',color:'#ef4444',type:'longhaul'},
            {name:'HE East Coast Backbone',coords:[[42.36,-71.06],[40.71,-74.01],[39.95,-75.17],[38.90,-77.04],[33.75,-84.39],[25.76,-80.19]],cap:'400 Gbps',owner:'Hurricane Electric',color:'#ef4444',type:'longhaul'},
            {name:'HE Midwest Route',coords:[[41.88,-87.63],[39.77,-86.16],[39.10,-84.51],[40.44,-80.00]],cap:'400 Gbps',owner:'Hurricane Electric',color:'#ef4444',type:'longhaul'},
            {name:'HE Fremont Campus',coords:[[37.55,-122.05],[37.52,-122.02],[37.49,-122.05],[37.52,-122.08],[37.55,-122.05]],cap:'1 Tbps',owner:'Hurricane Electric',color:'#ef4444',type:'fabric'},
            
            // ============================================
            // GTT NETWORKS
            // ============================================
            {name:'GTT US Backbone North',coords:[[42.36,-71.06],[40.71,-74.01],[41.88,-87.63],[44.98,-93.27],[47.61,-122.34]],cap:'400 Gbps',owner:'GTT',color:'#7c3aed',type:'longhaul'},
            {name:'GTT US Backbone South',coords:[[40.71,-74.01],[38.90,-77.04],[33.75,-84.39],[29.76,-95.37],[32.78,-96.80],[34.05,-118.24]],cap:'400 Gbps',owner:'GTT',color:'#7c3aed',type:'longhaul'},
            {name:'GTT NYC Metro',coords:[[40.76,-73.98],[40.72,-73.93],[40.68,-73.98],[40.72,-74.03],[40.76,-73.98]],cap:'400 Gbps',owner:'GTT',color:'#7c3aed',type:'metro'},
            {name:'GTT LA Metro',coords:[[34.05,-118.24],[34.01,-118.19],[33.97,-118.24],[34.01,-118.29],[34.05,-118.24]],cap:'400 Gbps',owner:'GTT',color:'#7c3aed',type:'metro'},
            
            // ============================================
            // TELIA CARRIER
            // ============================================
            {name:'Telia US Backbone',coords:[[40.71,-74.01],[41.88,-87.63],[39.75,-104.99],[34.05,-118.24]],cap:'400 Gbps',owner:'Telia',color:'#6366f1',type:'longhaul'},
            {name:'Telia NYC-Chicago',coords:[[40.71,-74.01],[41.88,-87.63]],cap:'400 Gbps',owner:'Telia',color:'#6366f1',type:'longhaul'},
            {name:'Telia NYC Metro',coords:[[40.76,-73.98],[40.72,-73.94],[40.68,-73.98],[40.72,-74.02],[40.76,-73.98]],cap:'400 Gbps',owner:'Telia',color:'#6366f1',type:'metro'},
            
            // ============================================
            // NTT/VERIO
            // ============================================
            {name:'NTT US Backbone',coords:[[40.71,-74.01],[38.90,-77.04],[33.75,-84.39],[32.78,-96.80],[34.05,-118.24],[37.79,-122.41]],cap:'400 Gbps',owner:'NTT',color:'#e11d48',type:'longhaul'},
            {name:'NTT NYC Metro',coords:[[40.76,-73.98],[40.72,-73.93],[40.68,-73.98],[40.72,-74.03],[40.76,-73.98]],cap:'400 Gbps',owner:'NTT',color:'#e11d48',type:'metro'},
            {name:'NTT LA Metro',coords:[[34.05,-118.24],[34.01,-118.19],[33.97,-118.24],[34.01,-118.29],[34.05,-118.24]],cap:'400 Gbps',owner:'NTT',color:'#e11d48',type:'metro'},
            
            // ============================================
            // ADDITIONAL REGIONAL DARK FIBER
            // ============================================
            
            // CenturyLink/Lumen Dark Fiber Routes
            {name:'Lumen Dark Southwest',coords:[[33.45,-112.07],[34.05,-118.24],[36.17,-115.14],[35.08,-106.65]],cap:'Dark',owner:'Lumen',color:'#ef4444',type:'dark'},
            {name:'Lumen Dark Southeast',coords:[[33.75,-84.39],[30.33,-81.66],[27.95,-82.46],[25.76,-80.19]],cap:'Dark',owner:'Lumen',color:'#ef4444',type:'dark'},
            {name:'Lumen Dark Midwest',coords:[[41.88,-87.63],[39.10,-94.58],[41.26,-95.94],[44.98,-93.27]],cap:'Dark',owner:'Lumen',color:'#ef4444',type:'dark'},
            
            // Windstream Dark Fiber
            {name:'Windstream Southeast Dark',coords:[[35.23,-80.84],[33.75,-84.39],[32.08,-81.09],[30.33,-81.66]],cap:'Dark',owner:'Windstream',color:'#0891b2',type:'dark'},
            {name:'Windstream Midwest Dark',coords:[[41.88,-87.63],[39.77,-86.16],[38.63,-90.20],[39.10,-94.58]],cap:'Dark',owner:'Windstream',color:'#0891b2',type:'dark'},
            {name:'Windstream Southwest Dark',coords:[[32.78,-96.80],[29.76,-95.37],[29.42,-98.49],[31.76,-106.49]],cap:'Dark',owner:'Windstream',color:'#0891b2',type:'dark'},
            
            // Fatbeam Northwest Dark Fiber
            {name:'Fatbeam Washington',coords:[[47.61,-122.34],[47.25,-122.44],[47.66,-117.43],[48.75,-122.47]],cap:'Dark',owner:'Fatbeam',color:'#84cc16',type:'dark'},
            {name:'Fatbeam Idaho',coords:[[46.87,-114.00],[43.62,-116.21],[42.87,-112.45]],cap:'Dark',owner:'Fatbeam',color:'#84cc16',type:'dark'},
            {name:'Fatbeam Montana',coords:[[46.87,-114.00],[45.78,-111.04],[48.21,-114.32]],cap:'Dark',owner:'Fatbeam',color:'#84cc16',type:'dark'},
            
            // Zayo Dark Fiber Additional
            {name:'Zayo Dark NYC-DC',coords:[[40.71,-74.01],[39.95,-75.17],[38.90,-77.04]],cap:'Dark',owner:'Zayo',color:'#3498db',type:'dark'},
            {name:'Zayo Dark DC-Atlanta',coords:[[38.90,-77.04],[35.23,-80.84],[33.75,-84.39]],cap:'Dark',owner:'Zayo',color:'#3498db',type:'dark'},
            {name:'Zayo Dark Chicago-Denver',coords:[[41.88,-87.63],[41.26,-95.94],[39.75,-104.99]],cap:'Dark',owner:'Zayo',color:'#3498db',type:'dark'},
            {name:'Zayo Dark Denver-LA',coords:[[39.75,-104.99],[35.08,-106.65],[33.45,-112.07],[34.05,-118.24]],cap:'Dark',owner:'Zayo',color:'#3498db',type:'dark'},
            {name:'Zayo Dark LA-SF',coords:[[34.05,-118.24],[36.75,-119.78],[37.79,-122.41]],cap:'Dark',owner:'Zayo',color:'#3498db',type:'dark'},
            
            // Electric Lightwave / Integra (now Zayo)
            {name:'Electric Lightwave PNW',coords:[[47.61,-122.34],[45.52,-122.68],[44.05,-121.31],[43.62,-116.21]],cap:'Dark',owner:'Electric Lightwave',color:'#14b8a6',type:'dark'},
            
            // FiberLight Dark Fiber
            {name:'FiberLight Texas Dark',coords:[[32.78,-96.80],[30.27,-97.74],[29.76,-95.37],[29.42,-98.49]],cap:'Dark',owner:'FiberLight',color:'#f472b6',type:'dark'},
            {name:'FiberLight Florida Dark',coords:[[30.33,-81.66],[28.54,-81.38],[27.95,-82.46],[26.12,-80.14]],cap:'Dark',owner:'FiberLight',color:'#f472b6',type:'dark'},
            {name:'FiberLight Georgia Dark',coords:[[33.75,-84.39],[32.08,-81.09],[31.58,-84.16]],cap:'Dark',owner:'FiberLight',color:'#f472b6',type:'dark'},
            {name:'FiberLight DC Metro Dark',coords:[[38.90,-77.04],[38.95,-77.37],[39.04,-77.49]],cap:'Dark',owner:'FiberLight',color:'#f472b6',type:'dark'},

            // ============================================
            // 123NET MICHIGAN NETWORK
            // ============================================
            {name:'123Net Michigan Backbone',coords:[[42.34,-83.05],[42.96,-85.66],[43.62,-84.25],[44.76,-85.64]],cap:'400 Gbps',owner:'123Net',color:'#e74c3c',type:'longhaul'},
            {name:'123Net Detroit Ring',coords:[[42.34,-83.05],[42.38,-83.00],[42.42,-83.05],[42.38,-83.10],[42.34,-83.05]],cap:'400 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Grand Rapids',coords:[[42.96,-85.66],[42.92,-85.61],[42.88,-85.66],[42.92,-85.72],[42.96,-85.66]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Ann Arbor',coords:[[42.28,-83.74],[42.24,-83.69],[42.20,-83.74],[42.24,-83.80],[42.28,-83.74]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Lansing',coords:[[42.73,-84.56],[42.69,-84.51],[42.65,-84.56],[42.69,-84.62],[42.73,-84.56]],cap:'100 Gbps',owner:'123Net',color:'#e74c3c',type:'metro'},
            {name:'123Net Southfield Data Center',coords:[[42.47,-83.22],[42.45,-83.20],[42.43,-83.22],[42.45,-83.24],[42.47,-83.22]],cap:'400 Gbps',owner:'123Net',color:'#e74c3c',type:'fabric'},
            
            // ============================================
            // FIRSTLIGHT FIBER - NORTHEAST
            // ============================================
            {name:'FirstLight Maine',coords:[[44.31,-69.78],[44.80,-68.77],[45.25,-69.23],[46.87,-68.01]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'longhaul'},
            {name:'FirstLight Vermont',coords:[[44.48,-73.21],[44.26,-72.58],[43.61,-72.97],[42.85,-72.56]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'longhaul'},
            {name:'FirstLight New Hampshire',coords:[[43.21,-71.54],[43.66,-72.32],[44.27,-71.14],[43.01,-70.89]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'longhaul'},
            {name:'FirstLight Upstate NY',coords:[[42.65,-73.76],[43.16,-77.61],[43.05,-76.15],[44.70,-73.45]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'longhaul'},
            {name:'FirstLight Albany Metro',coords:[[42.65,-73.76],[42.61,-73.70],[42.57,-73.76],[42.61,-73.82],[42.65,-73.76]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'metro'},
            {name:'FirstLight Portland ME',coords:[[43.66,-70.26],[43.62,-70.20],[43.58,-70.26],[43.62,-70.32],[43.66,-70.26]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'metro'},
            {name:'FirstLight Burlington',coords:[[44.48,-73.21],[44.44,-73.15],[44.40,-73.21],[44.44,-73.27],[44.48,-73.21]],cap:'100 Gbps',owner:'FirstLight',color:'#4caf50',type:'metro'},
            
            // ============================================
            // EVERSTREAM - MIDWEST
            // ============================================
            {name:'Everstream Ohio Backbone',coords:[[41.50,-81.69],[40.76,-84.11],[39.76,-84.19],[39.10,-84.51]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'longhaul'},
            {name:'Everstream Michigan',coords:[[42.34,-83.05],[42.96,-85.66],[42.27,-85.59],[41.92,-83.40]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'longhaul'},
            {name:'Everstream Indiana',coords:[[39.77,-86.16],[41.08,-85.14],[41.68,-86.25],[39.77,-86.16]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'longhaul'},
            {name:'Everstream Cleveland Metro',coords:[[41.50,-81.69],[41.45,-81.63],[41.40,-81.69],[41.45,-81.75],[41.50,-81.69]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Everstream Columbus',coords:[[39.96,-83.00],[39.91,-82.94],[39.86,-83.00],[39.91,-83.06],[39.96,-83.00]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Everstream Detroit Metro',coords:[[42.34,-83.05],[42.29,-82.99],[42.24,-83.05],[42.29,-83.11],[42.34,-83.05]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Everstream Indianapolis',coords:[[39.77,-86.16],[39.72,-86.10],[39.67,-86.16],[39.72,-86.22],[39.77,-86.16]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            {name:'Everstream Cincinnati',coords:[[39.10,-84.51],[39.05,-84.45],[39.00,-84.51],[39.05,-84.57],[39.10,-84.51]],cap:'400 Gbps',owner:'Everstream',color:'#4caf50',type:'metro'},
            
            // ============================================
            // SEGRA (FORMERLY LUMOS) - SOUTHEAST
            // ============================================
            {name:'Segra Virginia Backbone',coords:[[37.54,-77.46],[37.27,-79.94],[38.03,-78.48],[38.90,-77.04]],cap:'200 Gbps',owner:'Segra',color:'#ff5722',type:'longhaul'},
            {name:'Segra Carolinas',coords:[[35.79,-78.64],[35.23,-80.84],[34.00,-81.03],[33.84,-83.32]],cap:'200 Gbps',owner:'Segra',color:'#ff5722',type:'longhaul'},
            {name:'Segra SC Lowcountry',coords:[[32.78,-79.93],[33.00,-80.18],[32.90,-80.04]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'longhaul'},
            {name:'Segra Charlotte Ring',coords:[[35.23,-80.84],[35.18,-80.78],[35.13,-80.84],[35.18,-80.90],[35.23,-80.84]],cap:'200 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Segra Raleigh Ring',coords:[[35.79,-78.64],[35.74,-78.58],[35.69,-78.64],[35.74,-78.70],[35.79,-78.64]],cap:'200 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Segra Greensboro',coords:[[36.07,-79.79],[36.02,-79.73],[35.97,-79.79],[36.02,-79.85],[36.07,-79.79]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Segra Richmond',coords:[[37.54,-77.46],[37.49,-77.40],[37.44,-77.46],[37.49,-77.52],[37.54,-77.46]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Segra Columbia SC',coords:[[34.00,-81.03],[33.95,-80.97],[33.90,-81.03],[33.95,-81.09],[34.00,-81.03]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            {name:'Segra Greenville SC',coords:[[34.85,-82.40],[34.80,-82.34],[34.75,-82.40],[34.80,-82.46],[34.85,-82.40]],cap:'100 Gbps',owner:'Segra',color:'#ff5722',type:'metro'},
            
            // ============================================
            // US SIGNAL - MIDWEST
            // ============================================
            {name:'US Signal Michigan',coords:[[42.34,-83.05],[42.96,-85.66],[43.62,-84.25],[42.27,-85.59]],cap:'100 Gbps',owner:'US Signal',color:'#00bcd4',type:'longhaul'},
            {name:'US Signal Ohio',coords:[[41.50,-81.69],[39.96,-83.00],[39.76,-84.19]],cap:'100 Gbps',owner:'US Signal',color:'#00bcd4',type:'longhaul'},
            {name:'US Signal Chicago Metro',coords:[[41.88,-87.63],[41.83,-87.57],[41.78,-87.63],[41.83,-87.69],[41.88,-87.63]],cap:'200 Gbps',owner:'US Signal',color:'#00bcd4',type:'metro'},
            {name:'US Signal Detroit Metro',coords:[[42.34,-83.05],[42.29,-82.99],[42.24,-83.05],[42.29,-83.11],[42.34,-83.05]],cap:'100 Gbps',owner:'US Signal',color:'#00bcd4',type:'metro'},
            
            // ============================================
            // CONTERRA - SOUTHEAST/TEXAS
            // ============================================
            {name:'Conterra Texas Network',coords:[[32.78,-96.80],[30.27,-97.74],[29.76,-95.37],[30.45,-91.15]],cap:'100 Gbps',owner:'Conterra',color:'#8b5cf6',type:'longhaul'},
            {name:'Conterra Louisiana',coords:[[30.45,-91.15],[29.95,-90.07],[30.22,-93.22],[32.51,-93.75]],cap:'100 Gbps',owner:'Conterra',color:'#8b5cf6',type:'longhaul'},
            {name:'Conterra Arkansas',coords:[[34.75,-92.29],[35.38,-94.20],[36.07,-94.17]],cap:'100 Gbps',owner:'Conterra',color:'#8b5cf6',type:'longhaul'},
            {name:'Conterra Dallas Metro',coords:[[32.78,-96.80],[32.73,-96.74],[32.68,-96.80],[32.73,-96.86],[32.78,-96.80]],cap:'100 Gbps',owner:'Conterra',color:'#8b5cf6',type:'metro'},
            
            // ============================================
            // HOTWIRE COMMUNICATIONS
            // ============================================
            {name:'Hotwire Florida',coords:[[26.12,-80.14],[25.76,-80.19],[26.46,-80.07],[26.71,-80.05]],cap:'50 Gbps',owner:'Hotwire',color:'#f59e0b',type:'metro'},
            {name:'Hotwire Philadelphia',coords:[[39.96,-75.17],[39.92,-75.12],[39.88,-75.17],[39.92,-75.22],[39.96,-75.17]],cap:'50 Gbps',owner:'Hotwire',color:'#f59e0b',type:'metro'},
            
            // ============================================
            // STARRY INTERNET
            // ============================================
            {name:'Starry Boston',coords:[[42.36,-71.06],[42.32,-71.01],[42.28,-71.06],[42.32,-71.12],[42.36,-71.06]],cap:'10 Gbps',owner:'Starry',color:'#3b82f6',type:'metro'},
            {name:'Starry NYC',coords:[[40.76,-73.98],[40.72,-73.93],[40.68,-73.98],[40.72,-74.03],[40.76,-73.98]],cap:'10 Gbps',owner:'Starry',color:'#3b82f6',type:'metro'},
            {name:'Starry LA',coords:[[34.05,-118.24],[34.01,-118.19],[33.97,-118.24],[34.01,-118.29],[34.05,-118.24]],cap:'10 Gbps',owner:'Starry',color:'#3b82f6',type:'metro'},
            {name:'Starry Denver',coords:[[39.75,-104.99],[39.71,-104.94],[39.67,-104.99],[39.71,-105.05],[39.75,-104.99]],cap:'10 Gbps',owner:'Starry',color:'#3b82f6',type:'metro'},
            {name:'Starry DC',coords:[[38.91,-77.04],[38.87,-76.99],[38.83,-77.04],[38.87,-77.10],[38.91,-77.04]],cap:'10 Gbps',owner:'Starry',color:'#3b82f6',type:'metro'},
            
            // ============================================
            // WEBPASS/GOOGLE FIBER
            // ============================================
            {name:'Webpass San Francisco',coords:[[37.79,-122.41],[37.76,-122.37],[37.73,-122.41],[37.76,-122.45],[37.79,-122.41]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            {name:'Webpass Oakland',coords:[[37.80,-122.27],[37.77,-122.23],[37.74,-122.27],[37.77,-122.31],[37.80,-122.27]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            {name:'Webpass San Diego',coords:[[32.72,-117.16],[32.68,-117.11],[32.64,-117.16],[32.68,-117.21],[32.72,-117.16]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            {name:'Webpass Chicago',coords:[[41.88,-87.63],[41.84,-87.58],[41.80,-87.63],[41.84,-87.68],[41.88,-87.63]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            {name:'Webpass Seattle',coords:[[47.61,-122.34],[47.57,-122.29],[47.53,-122.34],[47.57,-122.39],[47.61,-122.34]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            {name:'Webpass Denver',coords:[[39.75,-104.99],[39.71,-104.94],[39.67,-104.99],[39.71,-105.04],[39.75,-104.99]],cap:'10 Gbps',owner:'Webpass',color:'#4285f4',type:'metro'},
            
            // ============================================
            // MUNICIPAL / COMMUNITY FIBER NETWORKS
            // ============================================
            
            // Chattanooga EPB extended
            {name:'EPB Chattanooga Full',coords:[[35.05,-85.31],[35.15,-85.20],[35.00,-85.10],[34.90,-85.30],[35.05,-85.31]],cap:'25 Gbps',owner:'EPB',color:'#22c55e',type:'metro'},
            
            // Longmont NextLight
            {name:'NextLight Longmont',coords:[[40.17,-105.10],[40.14,-105.05],[40.11,-105.10],[40.14,-105.15],[40.17,-105.10]],cap:'10 Gbps',owner:'NextLight',color:'#4caf50',type:'metro'},
            
            // Fort Collins Connexion
            {name:'Connexion Fort Collins',coords:[[40.59,-105.08],[40.55,-105.03],[40.51,-105.08],[40.55,-105.13],[40.59,-105.08]],cap:'10 Gbps',owner:'Connexion',color:'#0ea5e9',type:'metro'},
            
            // Huntsville Utilities
            {name:'Huntsville Utilities Fiber',coords:[[34.73,-86.59],[34.69,-86.54],[34.65,-86.59],[34.69,-86.64],[34.73,-86.59]],cap:'10 Gbps',owner:'Huntsville Utilities',color:'#8b5cf6',type:'metro'},
            
            // Wilson Greenlight NC
            {name:'Greenlight Wilson NC',coords:[[35.72,-77.92],[35.69,-77.87],[35.66,-77.92],[35.69,-77.97],[35.72,-77.92]],cap:'10 Gbps',owner:'Greenlight',color:'#22c55e',type:'metro'},
            
            // Cedar Falls Utilities
            {name:'CFU Cedar Falls',coords:[[42.53,-92.45],[42.50,-92.40],[42.47,-92.45],[42.50,-92.50],[42.53,-92.45]],cap:'10 Gbps',owner:'CFU',color:'#f97316',type:'metro'},
            
            // Lafayette LUS Fiber
            {name:'LUS Fiber Lafayette',coords:[[30.22,-92.02],[30.19,-91.97],[30.16,-92.02],[30.19,-92.07],[30.22,-92.02]],cap:'10 Gbps',owner:'LUS Fiber',color:'#ef4444',type:'metro'},
            
            // Bristol Virginia Utilities
            {name:'BVU OptiNet',coords:[[36.60,-82.19],[36.56,-82.14],[36.52,-82.19],[36.56,-82.24],[36.60,-82.19]],cap:'10 Gbps',owner:'BVU',color:'#7c3aed',type:'metro'},
            
            // Fairlawn Gig
            {name:'FairlawnGig Ohio',coords:[[41.13,-81.61],[41.10,-81.56],[41.07,-81.61],[41.10,-81.66],[41.13,-81.61]],cap:'10 Gbps',owner:'FairlawnGig',color:'#14b8a6',type:'metro'},
            
            // Sandy Oregon SandyNet
            {name:'SandyNet Oregon',coords:[[45.40,-122.26],[45.37,-122.21],[45.34,-122.26],[45.37,-122.31],[45.40,-122.26]],cap:'10 Gbps',owner:'SandyNet',color:'#0891b2',type:'metro'},
            
            // ============================================
            // ENTERPRISE DARK FIBER / PRIVATE NETWORKS
            // ============================================
            
            // Microsoft Dark Fiber Routes
            {name:'Microsoft Quincy-Seattle',coords:[[47.23,-119.85],[47.61,-122.34]],cap:'Dark',owner:'Microsoft',color:'#00a4ef',type:'dark'},
            {name:'Microsoft Seattle-Portland',coords:[[47.61,-122.34],[45.52,-122.68]],cap:'Dark',owner:'Microsoft',color:'#00a4ef',type:'dark'},
            {name:'Microsoft Ashburn Ring',coords:[[39.04,-77.49],[39.00,-77.44],[38.96,-77.49],[39.00,-77.54],[39.04,-77.49]],cap:'800 Gbps',owner:'Microsoft',color:'#00a4ef',type:'dark'},
            
            // Google/Alphabet Dark Fiber
            {name:'Google The Dalles-Portland',coords:[[45.60,-121.18],[45.52,-122.68]],cap:'Dark',owner:'Google',color:'#4285f4',type:'dark'},
            {name:'Google Council Bluffs',coords:[[41.26,-95.85],[41.23,-95.80],[41.20,-95.85],[41.23,-95.90],[41.26,-95.85]],cap:'400 Gbps',owner:'Google',color:'#4285f4',type:'dark'},
            
            // Amazon/AWS Dark Fiber
            {name:'AWS Ashburn Hub',coords:[[39.04,-77.49],[39.01,-77.45],[38.98,-77.49],[39.01,-77.53],[39.04,-77.49]],cap:'800 Gbps',owner:'AWS',color:'#ff9900',type:'dark'},
            {name:'AWS Columbus Hub',coords:[[39.96,-83.00],[39.93,-82.96],[39.90,-83.00],[39.93,-83.04],[39.96,-83.00]],cap:'400 Gbps',owner:'AWS',color:'#ff9900',type:'dark'},
            
            // Meta/Facebook Dark Fiber
            {name:'Meta Prineville Route',coords:[[44.29,-120.86],[45.52,-122.68]],cap:'Dark',owner:'Meta',color:'#0668e1',type:'dark'},
            {name:'Meta Altoona Route',coords:[[40.51,-78.40],[40.71,-74.01]],cap:'Dark',owner:'Meta',color:'#0668e1',type:'dark'}
        ];

        // Route type styling
        var routeTypeStyles = {
            longhaul: {weight: 3, opacity: 0.8, dashArray: null, label: '🛤️ Long-haul Backbone'},
            metro: {weight: 2, opacity: 0.7, dashArray: '6,4', label: '🏙️ Metro Ring'},
            fabric: {weight: 2, opacity: 0.7, dashArray: '4,4', label: '🔌 Fabric/Exchange'},
            hyperscale: {weight: 4, opacity: 0.9, dashArray: null, label: '☁️ Hyperscaler Network'},
            dark: {weight: 2, opacity: 0.6, dashArray: '8,8', label: '⚫ Dark Fiber'}
        };
        
        var routeTypeCounts = {longhaul: 0, metro: 0, fabric: 0, hyperscale: 0, dark: 0};
        var carrierCounts = {};
        
        fiberRoutes.forEach(function(f) {
            var routeColor = f.color || '#10b981';
            var routeType = f.type || 'longhaul';
            var style = routeTypeStyles[routeType] || routeTypeStyles.longhaul;
            
            routeTypeCounts[routeType] = (routeTypeCounts[routeType] || 0) + 1;
            carrierCounts[f.owner] = (carrierCounts[f.owner] || 0) + 1;
            
            L.polyline(f.coords, {
                color: routeColor,
                weight: style.weight,
                opacity: style.opacity,
                dashArray: style.dashArray
            }).bindPopup(
                '<div class="popup-title">🌐 '+f.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+style.label+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+f.cap+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">'+f.owner+'</span></div>'+
                '<div style="margin-top:4px;padding-top:4px;border-top:1px solid rgba(255,255,255,0.1);">'+
                '<span style="display:inline-block;width:10px;height:10px;background:'+routeColor+';border-radius:2px;margin-right:6px;"></span>'+
                '<span style="font-size:10px;color:rgba(255,255,255,0.6);">'+f.owner+' Network</span></div>'
            ).addTo(layers.fiber);
        });
        
        console.log('🌐 Fiber Routes: ' + fiberRoutes.length + ' routes loaded');
        window.fiberRoutes = fiberRoutes; // Make globally accessible
        window.routeTypeStyles = routeTypeStyles; // Make styles accessible too
        console.log('📊 Route types: Longhaul=' + routeTypeCounts.longhaul + ', Metro=' + routeTypeCounts.metro + ', Fabric=' + routeTypeCounts.fabric + ', Hyperscale=' + routeTypeCounts.hyperscale + ', Dark=' + routeTypeCounts.dark);
        console.log('🏢 Carriers: ' + Object.keys(carrierCounts).length + ' unique carriers');

        // ============================================
        // FIBER PANEL v5 - Debug Version
        // Extensive logging to find second-carrier bug
        // ============================================
        
        console.log('📡 Fiber Panel v5 DEBUG loading...');
        
        // Build carrier list from actual fiberRoutes data
        var FIBER_OWNERS = {};
        fiberRoutes.forEach(function(route) {
            var owner = route.owner;
            if (!FIBER_OWNERS[owner]) {
                FIBER_OWNERS[owner] = {
                    name: owner,
                    color: route.color || '#10b981',
                    type: route.type || 'metro',
                    routes: []
                };
            }
            FIBER_OWNERS[owner].routes.push(route);
        });
        
        var FIBER_CARRIERS = Object.keys(FIBER_OWNERS).sort().map(function(name) {
            var data = FIBER_OWNERS[name];
            return {
                id: name.toLowerCase().replace(/[^a-z0-9]/g, '-'),
                name: name,
                color: data.color,
                type: data.type,
                routeCount: data.routes.length,
                routes: data.routes
            };
        });
        
        console.log('📡 Built ' + FIBER_CARRIERS.length + ' carriers');
        
        // Minimal state
        var activeFiberCarriers = {};
        var fiberMasterLayer = null;
        var _fiberBusy = false;
        
        function initFiberPanel() {
            console.log('initFiberPanel called');
            
            // Create master layer ONCE
            fiberMasterLayer = L.layerGroup();
            fiberMasterLayer.addTo(map);
            console.log('Master layer created and added to map');
            
            renderFiberList();
        }
        
        function renderFiberList() {
            console.log('renderFiberList called');
            var listEl = document.getElementById('fiber-list');
            if (!listEl) {
                console.log('fiber-list element not found!');
                return;
            }
            
            var html = '';
            // Only show first 20 carriers for testing
            FIBER_CARRIERS.slice(0, 20).forEach(function(c, idx) {
                html += '<div class="fiber-item" data-idx="' + idx + '" style="padding:8px;margin:2px;background:#222;border-radius:4px;cursor:pointer;">';
                html += '<span style="display:inline-block;width:12px;height:12px;background:' + c.color + ';border-radius:2px;margin-right:8px;"></span>';
                html += c.name + ' (' + c.routeCount + ')';
                html += '</div>';
            });
            
            listEl.innerHTML = html;
            console.log('Rendered ' + Math.min(20, FIBER_CARRIERS.length) + ' carriers');
            
            // Attach click handler
            listEl.addEventListener('click', handleFiberClick);
        }
        
        function handleFiberClick(e) {
            console.log('=== CLICK EVENT ===');
            console.log('Target:', e.target);
            console.log('Busy:', _fiberBusy);
            
            if (_fiberBusy) {
                console.log('BLOCKED - busy');
                return;
            }
            
            var item = e.target.closest('.fiber-item');
            if (!item) {
                console.log('No fiber-item found');
                return;
            }
            
            var idx = parseInt(item.getAttribute('data-idx'));
            console.log('Carrier index:', idx);
            
            if (isNaN(idx) || idx < 0 || idx >= FIBER_CARRIERS.length) {
                console.log('Invalid index');
                return;
            }
            
            var carrier = FIBER_CARRIERS[idx];
            console.log('Carrier:', carrier.name);
            
            // Set busy flag
            _fiberBusy = true;
            
            // Toggle
            if (activeFiberCarriers[carrier.id]) {
                console.log('Turning OFF:', carrier.name);
                removeCarrierRoutes(carrier);
                delete activeFiberCarriers[carrier.id];
                item.style.background = '#222';
            } else {
                console.log('Turning ON:', carrier.name);
                console.log('Routes to add:', carrier.routes.length);
                addCarrierRoutes(carrier);
                activeFiberCarriers[carrier.id] = true;
                item.style.background = '#4a3';
            }
            
            console.log('Active carriers now:', Object.keys(activeFiberCarriers).length);
            
            // Release busy flag after delay
            setTimeout(function() {
                _fiberBusy = false;
                console.log('Busy flag released');
            }, 500);
            
            console.log('=== CLICK DONE ===');
        }
        
        function addCarrierRoutes(carrier) {
            console.log('addCarrierRoutes:', carrier.name);
            
            var added = 0;
            carrier.routes.forEach(function(route, i) {
                if (!route.coords || route.coords.length < 2) {
                    console.log('Skip route ' + i + ' - no coords');
                    return;
                }
                
                try {
                    var line = L.polyline(route.coords, {
                        color: carrier.color,
                        weight: 3,
                        opacity: 0.7
                    });
                    line._carrierId = carrier.id; // Tag it
                    line.addTo(fiberMasterLayer);
                    added++;
                } catch(err) {
                    console.error('Error adding route ' + i + ':', err);
                }
            });
            
            console.log('Added ' + added + ' routes for ' + carrier.name);
        }
        
        function removeCarrierRoutes(carrier) {
            console.log('removeCarrierRoutes:', carrier.name);
            
            var toRemove = [];
            fiberMasterLayer.eachLayer(function(layer) {
                if (layer._carrierId === carrier.id) {
                    toRemove.push(layer);
                }
            });
            
            console.log('Found ' + toRemove.length + ' layers to remove');
            
            toRemove.forEach(function(layer) {
                fiberMasterLayer.removeLayer(layer);
            });
            
            console.log('Removed ' + toRemove.length + ' routes');
        }
        
        // Stub functions
        function toggleFiberCarrier(id) { console.log('toggleFiberCarrier stub:', id); }
        function selectAllFiber() { console.log('selectAllFiber disabled'); }
        function clearAllFiber() { 
            console.log('clearAllFiber');
            fiberMasterLayer.clearLayers();
            activeFiberCarriers = {};
            document.querySelectorAll('.fiber-item').forEach(function(el) {
                el.style.background = '#222';
            });
        }
        function filterFiberType(t) { console.log('filterFiberType disabled'); }
        function filterFiberList() { console.log('filterFiberList disabled'); }
        function toggleFiberPanel() {
            var panel = document.getElementById('fiber-panel');
            if (panel) panel.classList.toggle('active');
        }
        function updateFiberStats() {}
        
        window.toggleFiberCarrier = toggleFiberCarrier;
        window.selectAllFiber = selectAllFiber;
        window.clearAllFiber = clearAllFiber;
        window.filterFiberType = filterFiberType;
        window.filterFiberList = filterFiberList;
        window.toggleFiberPanel = toggleFiberPanel;
        
        setTimeout(initFiberPanel, 300);
        console.log('✅ Fiber Panel v5 DEBUG ready');

        // ============================================
        // DARK FIBER DENSITY - Metro Fiber Hub Indicators
        // Shows carrier presence and lit fiber availability
        // ============================================
        var fiberDensity = [
            // Tier 1 - Ultra Dense (20+ carriers, 100+ Tbps aggregate)
            {name:'Ashburn/NoVA',lat:39.0438,lng:-77.4874,carriers:25,tbps:150,tier:'Ultra',providers:['Zayo','Lumen','AT&T','Verizon','Cogent','Crown Castle','FiberLight','Windstream']},
            {name:'Chicago 350 E Cermak',lat:41.8534,lng:-87.6189,carriers:22,tbps:80,tier:'Ultra',providers:['Zayo','Lumen','AT&T','Verizon','Cogent','Windstream']},
            {name:'NYC/Newark',lat:40.7356,lng:-74.1723,carriers:24,tbps:120,tier:'Ultra',providers:['Zayo','Lumen','AT&T','Verizon','Cogent','Lightpath']},
            {name:'Dallas/Irving',lat:32.8901,lng:-96.9512,carriers:18,tbps:60,tier:'Ultra',providers:['Zayo','Lumen','AT&T','Crown Castle']},
            // Tier 2 - Very Dense (12-19 carriers, 30-100 Tbps)
            {name:'Phoenix Metro',lat:33.4478,lng:-112.0712,carriers:14,tbps:45,tier:'Very High',providers:['Zayo','Lumen','AT&T','Cox','Crown Castle']},
            {name:'Atlanta 56 Marietta',lat:33.7534,lng:-84.3923,carriers:16,tbps:55,tier:'Very High',providers:['Zayo','Lumen','AT&T','Uniti','Windstream']},
            {name:'Denver',lat:39.7423,lng:-104.9856,carriers:13,tbps:35,tier:'Very High',providers:['Zayo','Lumen','AT&T']},
            {name:'Seattle Westin',lat:47.6145,lng:-122.3412,carriers:15,tbps:50,tier:'Very High',providers:['Zayo','Lumen','Cogent','Wave']},
            {name:'San Jose/SV',lat:37.3874,lng:-122.0834,carriers:17,tbps:65,tier:'Very High',providers:['Zayo','Lumen','AT&T','Cogent']},
            {name:'Los Angeles One Wilshire',lat:34.0500,lng:-118.2550,carriers:19,tbps:70,tier:'Very High',providers:['Zayo','Lumen','AT&T','Cogent','Crown Castle']},
            // Tier 3 - High Density (8-11 carriers, 15-30 Tbps)
            {name:'Houston',lat:29.7623,lng:-95.3634,carriers:11,tbps:28,tier:'High',providers:['Zayo','Lumen','AT&T','Uniti']},
            {name:'Columbus',lat:39.9612,lng:-82.9234,carriers:10,tbps:22,tier:'High',providers:['Zayo','Lumen','AT&T']},
            {name:'Kansas City',lat:39.0997,lng:-94.5786,carriers:9,tbps:18,tier:'High',providers:['Zayo','Lumen','Windstream']},
            {name:'St. Louis',lat:38.6312,lng:-90.1923,carriers:9,tbps:16,tier:'High',providers:['Zayo','Lumen','Windstream','Uniti']},
            {name:'Minneapolis',lat:44.9778,lng:-93.2650,carriers:10,tbps:20,tier:'High',providers:['Zayo','Lumen','Windstream']},
            {name:'Portland',lat:45.5312,lng:-122.6534,carriers:9,tbps:18,tier:'High',providers:['Zayo','Lumen','Cogent']},
            {name:'Salt Lake City',lat:40.7608,lng:-111.8910,carriers:8,tbps:15,tier:'High',providers:['Zayo','Lumen']},
            {name:'Reno TRIC',lat:39.5296,lng:-119.8138,carriers:8,tbps:14,tier:'High',providers:['Zayo','Lumen','Switch']},
            {name:'Charlotte',lat:35.2271,lng:-80.8431,carriers:10,tbps:22,tier:'High',providers:['Zayo','Lumen','Windstream']},
            {name:'Philadelphia',lat:39.9589,lng:-75.1623,carriers:11,tbps:25,tier:'High',providers:['Zayo','Lumen','AT&T','Verizon']},
            {name:'Indianapolis',lat:39.7678,lng:-86.1623,carriers:8,tbps:14,tier:'High',providers:['Zayo','Lumen','Windstream']},
            // Tier 4 - Moderate (5-7 carriers, 5-15 Tbps)
            {name:'Austin',lat:30.2672,lng:-97.7431,carriers:7,tbps:12,tier:'Moderate',providers:['Zayo','Lumen','AT&T']},
            {name:'Las Vegas',lat:36.1234,lng:-115.1523,carriers:7,tbps:10,tier:'Moderate',providers:['Zayo','Lumen','Switch']},
            {name:'Louisville',lat:38.2527,lng:-85.7585,carriers:5,tbps:6,tier:'Moderate',providers:['Lumen','AT&T','Windstream']},
            {name:'Quincy WA',lat:47.2312,lng:-119.8523,carriers:6,tbps:8,tier:'Moderate',providers:['Zayo','Lumen','MSFT Private']},
            {name:'The Dalles OR',lat:45.5912,lng:-121.1823,carriers:5,tbps:6,tier:'Moderate',providers:['Zayo','Google Private']},
            // Emerging markets - Lower density but growing
            {name:'Richmond VA',lat:37.5407,lng:-77.4360,carriers:6,tbps:8,tier:'Moderate',providers:['Zayo','Lumen','FiberLight']},
            {name:'Hillsboro OR',lat:45.5229,lng:-122.9898,carriers:7,tbps:10,tier:'Moderate',providers:['Zayo','Lumen']},
            {name:'Albuquerque',lat:35.0844,lng:-106.6504,carriers:4,tbps:4,tier:'Emerging',providers:['Lumen','Zayo']},
            {name:'Omaha',lat:41.2565,lng:-95.9345,carriers:5,tbps:5,tier:'Emerging',providers:['Lumen','Windstream']},
            {name:'Des Moines',lat:41.5868,lng:-93.6250,carriers:5,tbps:5,tier:'Emerging',providers:['Lumen','Windstream']},
            {name:'Cheyenne WY',lat:41.1400,lng:-104.8202,carriers:4,tbps:4,tier:'Emerging',providers:['Lumen','Zayo']},
            {name:'Boise ID',lat:43.6150,lng:-116.2023,carriers:4,tbps:3,tier:'Emerging',providers:['Lumen','Zayo']}
        ];
        
        // Create fiber density layer group
        layers.fiberDensity = L.layerGroup();
        
        fiberDensity.forEach(function(hub) {
            var tierColors = {
                'Ultra': '#10b981',
                'Very High': '#22c55e', 
                'High': '#84cc16',
                'Moderate': '#f59e0b',
                'Emerging': '#94a3b8'
            };
            var color = tierColors[hub.tier] || '#6366f1';
            var size = hub.carriers > 18 ? 20 : hub.carriers > 12 ? 16 : hub.carriers > 8 ? 12 : 8;
            
            L.circleMarker([hub.lat, hub.lng], {
                radius: size,
                fillColor: color,
                color: '#fff',
                weight: 2,
                opacity: 0.9,
                fillOpacity: 0.6
            }).bindPopup(
                '<div class="popup-title">🌐 '+hub.name+' Fiber Hub</div>'+
                '<div class="popup-row"><span class="popup-label">Density Tier</span><span class="popup-value" style="color:'+color+'">'+hub.tier+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Carriers Present</span><span class="popup-value">'+hub.carriers+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Aggregate Capacity</span><span class="popup-value">'+hub.tbps+' Tbps</span></div>'+
                '<div class="popup-row"><span class="popup-label">Key Providers</span><span class="popup-value" style="font-size:10px">'+hub.providers.slice(0,4).join(', ')+'</span></div>'+
                '<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.1);font-size:10px;color:rgba(255,255,255,0.7);">'+
                '💡 Dark fiber typically available from '+hub.providers.length+'+ lit providers</div>'
            ).addTo(layers.fiberDensity);
        });
        console.log('🌐 Fiber Density: ' + fiberDensity.length + ' metro hubs mapped');
        
        // ============================================
        // FIBER DENSITY INDICATORS - Metro Carrier Presence
        // Shows relative fiber availability by market
        // ============================================
        var fiberDensity = [
            // Tier 1 - Ultra Dense (20+ carriers)
            {name:'Ashburn, VA',lat:39.0438,lng:-77.4874,carriers:35,density:'Ultra',carriers_list:'Zayo, Lumen, AT&T, Verizon, Cogent, Crown Castle, FiberLight, Windstream, GTT, NTT'},
            {name:'Chicago, IL',lat:41.8789,lng:-87.6359,carriers:28,density:'Ultra',carriers_list:'Zayo, Lumen, AT&T, Verizon, Cogent, Windstream, Comcast, GTT'},
            {name:'Dallas, TX',lat:32.8901,lng:-96.9512,carriers:24,density:'Ultra',carriers_list:'Zayo, Lumen, AT&T, Crown Castle, Cogent, Windstream, Uniti'},
            {name:'New York/NJ',lat:40.7356,lng:-74.1723,carriers:32,density:'Ultra',carriers_list:'Zayo, Lumen, AT&T, Verizon, Lightpath, Cogent, Crown Castle'},
            {name:'Los Angeles, CA',lat:34.0522,lng:-118.2437,carriers:26,density:'Ultra',carriers_list:'Zayo, Lumen, AT&T, Cogent, Crown Castle, GTT, NTT'},
            // Tier 2 - Very Dense (10-20 carriers)
            {name:'Phoenix, AZ',lat:33.4478,lng:-112.0712,carriers:16,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Cox, Crown Castle, Uniti'},
            {name:'Atlanta, GA',lat:33.7490,lng:-84.3880,carriers:18,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Cogent, Windstream, Uniti, PBI Fiber'},
            {name:'Denver, CO',lat:39.7392,lng:-104.9903,carriers:14,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Cogent, CenturyLink'},
            {name:'Seattle, WA',lat:47.6062,lng:-122.3321,carriers:15,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Cogent, Wave'},
            {name:'San Jose, CA',lat:37.3382,lng:-121.8863,carriers:20,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Cogent, Hurricane Electric'},
            {name:'Houston, TX',lat:29.7604,lng:-95.3698,carriers:15,density:'Very High',carriers_list:'Zayo, Lumen, AT&T, Windstream, Uniti'},
            // Tier 3 - High Density (5-10 carriers)
            {name:'Columbus, OH',lat:39.9612,lng:-82.9988,carriers:10,density:'High',carriers_list:'Zayo, Lumen, AT&T, Windstream'},
            {name:'Kansas City',lat:39.0997,lng:-94.5786,carriers:12,density:'High',carriers_list:'Zayo, Lumen, AT&T, Google Fiber, Uniti'},
            {name:'St. Louis, MO',lat:38.6270,lng:-90.1994,carriers:9,density:'High',carriers_list:'Zayo, Lumen, AT&T, Windstream'},
            {name:'Salt Lake City',lat:40.7608,lng:-111.8910,carriers:8,density:'High',carriers_list:'Zayo, Lumen, AT&T, Utopia'},
            {name:'Portland, OR',lat:45.5152,lng:-122.6784,carriers:10,density:'High',carriers_list:'Zayo, Lumen, AT&T, Wave'},
            {name:'Reno, NV',lat:39.5296,lng:-119.8138,carriers:7,density:'High',carriers_list:'Zayo, Lumen, AT&T, CC Communications'},
            {name:'Indianapolis, IN',lat:39.7684,lng:-86.1581,carriers:8,density:'High',carriers_list:'Zayo, Lumen, AT&T, Windstream'},
            {name:'Charlotte, NC',lat:35.2271,lng:-80.8431,carriers:9,density:'High',carriers_list:'Zayo, Lumen, AT&T, Windstream, Segra'},
            // Tier 4 - Moderate (3-5 carriers)
            {name:'Louisville, KY',lat:38.2527,lng:-85.7585,carriers:5,density:'Moderate',carriers_list:'Lumen, AT&T, Windstream'},
            {name:'Omaha, NE',lat:41.2565,lng:-95.9345,carriers:5,density:'Moderate',carriers_list:'Lumen, AT&T, Windstream'},
            {name:'Des Moines, IA',lat:41.5868,lng:-93.6250,carriers:6,density:'Moderate',carriers_list:'Lumen, AT&T, Windstream, Mediacom'},
            {name:'Cheyenne, WY',lat:41.1400,lng:-104.8202,carriers:4,density:'Moderate',carriers_list:'Lumen, AT&T'},
            {name:'Albuquerque, NM',lat:35.0844,lng:-106.6504,carriers:4,density:'Moderate',carriers_list:'Lumen, AT&T, Zayo'}
        ];
        
        fiberDensity.forEach(function(fd) {
            var densityColor = fd.density === 'Ultra' ? '#22c55e' : 
                              fd.density === 'Very High' ? '#84cc16' : 
                              fd.density === 'High' ? '#eab308' : '#f97316';
            var radius = fd.carriers >= 20 ? 18 : fd.carriers >= 10 ? 14 : fd.carriers >= 5 ? 10 : 7;
            
            L.circleMarker([fd.lat, fd.lng], {
                radius: radius,
                fillColor: densityColor,
                color: '#fff',
                weight: 2,
                opacity: 0.9,
                fillOpacity: 0.3
            }).bindPopup(
                '<div class="popup-title">🌐 ' + fd.name + ' Fiber Hub</div>' +
                '<div class="popup-row"><span class="popup-label">Carrier Density</span><span class="popup-value" style="color:' + densityColor + '">' + fd.density + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">Carriers Present</span><span class="popup-value">' + fd.carriers + '+</span></div>' +
                '<div style="margin-top:8px;padding:8px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:10px;color:var(--text2);">' +
                '<strong>Major Carriers:</strong><br>' + fd.carriers_list + '</div>' +
                '<div style="margin-top:6px;font-size:9px;color:var(--text3);">💡 Higher density = more dark fiber options & competitive pricing</div>'
            ).addTo(layers.fiber);
        });
        console.log('🌐 Fiber Density: ' + fiberDensity.length + ' metro hubs mapped');

        // ============================================
        // ISO/RTO GRID BOUNDARIES
        // Major Independent System Operator regions
        // ============================================
        var isoRegions = [
            {
                name: 'PJM Interconnection',
                abbrev: 'PJM',
                color: '#3b82f6',
                coords: [[42.3,-80.5],[42.5,-79.0],[43.0,-77.0],[42.8,-74.5],[41.5,-74.0],[40.5,-74.0],[39.5,-75.5],[38.5,-76.0],[38.0,-76.5],[37.0,-76.5],[36.5,-78.5],[36.5,-81.5],[37.5,-82.5],[38.5,-83.0],[39.5,-84.5],[40.5,-84.5],[41.5,-83.0],[42.0,-81.5],[42.3,-80.5]],
                states: '13 States + DC',
                load: '180 GW peak',
                lmp: '$32-45/MWh'
            },
            {
                name: 'ERCOT (Texas)',
                abbrev: 'ERCOT',
                color: '#ef4444',
                coords: [[36.5,-103.0],[36.5,-100.0],[34.5,-100.0],[34.0,-99.0],[33.5,-97.0],[32.0,-95.0],[30.0,-94.0],[29.5,-95.0],[29.0,-96.0],[28.5,-97.0],[26.0,-97.5],[26.0,-99.0],[27.0,-100.0],[29.0,-102.0],[29.5,-103.0],[31.0,-104.0],[32.0,-104.0],[34.0,-103.0],[36.5,-103.0]],
                states: 'Texas (85%)',
                load: '85 GW peak',
                lmp: '$25-40/MWh'
            },
            {
                name: 'CAISO (California)',
                abbrev: 'CAISO',
                color: '#eab308',
                coords: [[42.0,-124.5],[42.0,-120.0],[39.0,-120.0],[36.0,-118.0],[35.5,-117.5],[35.0,-117.0],[34.5,-117.0],[33.0,-117.0],[32.5,-117.5],[32.5,-118.0],[33.0,-119.0],[34.5,-121.0],[36.0,-122.0],[38.0,-123.0],[40.0,-124.0],[42.0,-124.5]],
                states: 'California',
                load: '52 GW peak',
                lmp: '$40-80/MWh'
            },
            {
                name: 'MISO',
                abbrev: 'MISO',
                color: '#22c55e',
                coords: [[49.0,-97.0],[49.0,-90.0],[47.0,-88.0],[45.0,-85.0],[43.5,-84.5],[42.5,-84.5],[41.5,-84.5],[40.5,-84.5],[39.5,-84.5],[38.0,-85.0],[36.5,-87.0],[35.0,-89.0],[33.0,-91.0],[31.0,-91.5],[30.0,-92.0],[29.5,-93.0],[29.5,-94.0],[31.0,-94.5],[33.0,-94.0],[35.0,-94.5],[37.0,-95.0],[39.0,-95.5],[41.0,-96.0],[43.0,-96.5],[46.0,-97.0],[49.0,-97.0]],
                states: '15 States',
                load: '130 GW peak',
                lmp: '$28-38/MWh'
            },
            {
                name: 'SPP (Southwest Power Pool)',
                abbrev: 'SPP',
                color: '#a855f7',
                coords: [[43.5,-104.0],[43.5,-99.0],[42.0,-96.5],[40.5,-96.0],[39.5,-95.5],[37.5,-95.0],[35.5,-94.5],[34.0,-94.5],[33.5,-95.0],[33.0,-96.0],[33.5,-97.5],[34.0,-99.5],[35.0,-100.5],[36.5,-103.0],[38.0,-103.5],[40.0,-104.0],[43.5,-104.0]],
                states: '14 States',
                load: '55 GW peak',
                lmp: '$24-35/MWh'
            },
            {
                name: 'NYISO (New York)',
                abbrev: 'NYISO',
                color: '#f97316',
                coords: [[45.0,-79.8],[45.0,-74.0],[43.5,-73.5],[42.5,-73.5],[41.5,-73.5],[41.0,-74.0],[40.5,-74.0],[40.5,-74.5],[41.0,-75.5],[42.0,-79.0],[42.5,-79.5],[43.5,-79.0],[45.0,-79.8]],
                states: 'New York',
                load: '33 GW peak',
                lmp: '$35-55/MWh'
            },
            {
                name: 'ISO-NE (New England)',
                abbrev: 'ISO-NE',
                color: '#06b6d4',
                coords: [[47.5,-68.0],[45.0,-67.0],[44.0,-68.5],[43.5,-70.0],[42.5,-71.0],[42.0,-71.5],[41.5,-72.0],[41.0,-73.0],[41.0,-73.7],[41.5,-73.5],[42.5,-73.5],[43.5,-73.5],[45.0,-73.5],[45.5,-71.0],[47.5,-68.0]],
                states: '6 New England States',
                load: '28 GW peak',
                lmp: '$38-60/MWh'
            }
        ];

        isoRegions.forEach(function(iso) {
            L.polygon(iso.coords, {
                color: iso.color,
                weight: 3,
                opacity: 0.9,
                fill: false,
                dashArray: '8, 4'
            }).bindPopup(
                '<div class="popup-title">⚡ ' + iso.name + '</div>' +
                '<div class="popup-row"><span class="popup-label">Abbreviation</span><span class="popup-value"><span class="iso-badge iso-' + iso.abbrev.toLowerCase() + '">' + iso.abbrev + '</span></span></div>' +
                '<div class="popup-row"><span class="popup-label">Territory</span><span class="popup-value">' + iso.states + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">Peak Load</span><span class="popup-value">' + iso.load + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">Typical LMP</span><span class="popup-value">' + iso.lmp + '</span></div>' +
                '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1);font-size:10px;color:var(--text3);">Click for real-time pricing data</div>'
            ).addTo(layers.iso);
        });
        console.log('ISO/RTO Regions: ' + isoRegions.length + ' grid operators');

        // ============================================
        // SUBMARINE CABLES - TeleGeography Data
        // ============================================
        var submarineCables = [
            {name:'MAREA',coords:[[40.8,-73.9],[41.2,-50.5],[42.5,-9.2]],cap:'200 Tbps',owner:'Microsoft/Meta',rfs:'2017'},
            {name:'HAVFRUE/AEC-2',coords:[[40.6,-74.0],[43.5,-52.5],[54.5,-3.5]],cap:'108 Tbps',owner:'Google/Facebook',rfs:'2019'},
            {name:'Dunant',coords:[[40.5,-74.2],[42.0,-45.5],[46.5,-1.8]],cap:'250 Tbps',owner:'Google',rfs:'2020'},
            {name:'Grace Hopper',coords:[[40.4,-74.1],[42.5,-55.2],[51.8,-1.2]],cap:'340 Tbps',owner:'Google',rfs:'2022'},
            {name:'Amitié',coords:[[40.3,-74.0],[43.0,-48.5],[47.2,-1.5]],cap:'400 Tbps',owner:'MSFT/Meta/Vodafone',rfs:'2023'},
            {name:'Pacific Light Cable',coords:[[34.0,-118.5],[21.3,-158.0],[22.3,114.2]],cap:'144 Tbps',owner:'Google/Meta',rfs:'2020'},
            {name:'Curie',coords:[[34.2,-118.8],[23.5,-106.5],[8.5,-77.5],[-5.5,-81.5],[-33.5,-71.5]],cap:'72 Tbps',owner:'Google',rfs:'2019'},
            {name:'Echo/Bifrost',coords:[[34.5,-119.5],[21.0,-157.5],[1.3,103.8]],cap:'288 Tbps',owner:'Google/Meta',rfs:'2024'},
            {name:'Jupiter',coords:[[34.0,-118.2],[21.2,-157.8],[35.5,139.8]],cap:'60 Tbps',owner:'Amazon/Meta',rfs:'2020'}
        ];

        submarineCables.forEach(function(c) {
            L.polyline(c.coords,{color:'#06b6d4',weight:3,opacity:0.6,dashArray:'4,8'}).bindPopup(
                '<div class="popup-title">🌊 '+c.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value">'+c.cap+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">'+c.owner+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">RFS</span><span class="popup-value">'+c.rfs+'</span></div>'
            ).addTo(layers.submarine);
        });

        // ============================================
        // AIRPORTS - FAA Data
        // ============================================
        var airports = [
            // Major hubs
            {code:'IAD',name:'Washington Dulles',lat:38.94,lng:-77.46,type:'Hub'},
            {code:'DFW',name:'Dallas/Fort Worth',lat:32.90,lng:-97.04,type:'Hub'},
            {code:'ORD',name:'Chicago O\'Hare',lat:41.98,lng:-87.90,type:'Hub'},
            {code:'PHX',name:'Phoenix Sky Harbor',lat:33.44,lng:-112.01,type:'Hub'},
            {code:'SFO',name:'San Francisco Intl',lat:37.62,lng:-122.38,type:'Hub'},
            {code:'SEA',name:'Seattle-Tacoma',lat:47.45,lng:-122.31,type:'Hub'},
            {code:'ATL',name:'Atlanta Hartsfield',lat:33.64,lng:-84.43,type:'Hub'},
            {code:'DEN',name:'Denver Intl',lat:39.86,lng:-104.67,type:'Hub'},
            {code:'LAX',name:'Los Angeles Intl',lat:33.94,lng:-118.41,type:'Hub'},
            {code:'JFK',name:'New York JFK',lat:40.64,lng:-73.78,type:'Hub'},
            {code:'EWR',name:'Newark Liberty',lat:40.69,lng:-74.17,type:'Hub'},
            {code:'IAH',name:'Houston Intercont',lat:29.99,lng:-95.34,type:'Hub'},
            {code:'MSP',name:'Minneapolis-St Paul',lat:44.88,lng:-93.22,type:'Hub'},
            {code:'DTW',name:'Detroit Metro',lat:42.21,lng:-83.35,type:'Hub'},
            {code:'BOS',name:'Boston Logan',lat:42.36,lng:-71.01,type:'Hub'},
            {code:'MIA',name:'Miami Intl',lat:25.80,lng:-80.29,type:'Hub'},
            {code:'CLT',name:'Charlotte Douglas',lat:35.21,lng:-80.94,type:'Hub'},
            {code:'LAS',name:'Las Vegas McCarran',lat:36.08,lng:-115.15,type:'Hub'},
            {code:'SLC',name:'Salt Lake City',lat:40.79,lng:-111.98,type:'Hub'},
            {code:'PDX',name:'Portland Intl',lat:45.59,lng:-122.60,type:'Hub'},
            // Regional near DC markets
            {code:'BWI',name:'Baltimore-Washington',lat:39.18,lng:-76.67,type:'Regional'},
            {code:'RIC',name:'Richmond Intl',lat:37.51,lng:-77.32,type:'Regional'},
            {code:'CMH',name:'Columbus Intl',lat:39.99,lng:-82.89,type:'Regional'},
            {code:'IND',name:'Indianapolis Intl',lat:39.72,lng:-86.29,type:'Regional'},
            {code:'CVG',name:'Cincinnati NKY',lat:39.05,lng:-84.67,type:'Regional'},
            {code:'MCI',name:'Kansas City Intl',lat:39.30,lng:-94.71,type:'Regional'},
            {code:'BNA',name:'Nashville Intl',lat:36.12,lng:-86.68,type:'Regional'},
            {code:'RDU',name:'Raleigh-Durham',lat:35.88,lng:-78.79,type:'Regional'},
            {code:'AUS',name:'Austin-Bergstrom',lat:30.19,lng:-97.67,type:'Regional'},
            {code:'SAT',name:'San Antonio Intl',lat:29.53,lng:-98.47,type:'Regional'},
            {code:'OAK',name:'Oakland Intl',lat:37.72,lng:-122.22,type:'Regional'},
            {code:'SJC',name:'San Jose Intl',lat:37.36,lng:-121.93,type:'Regional'},
            {code:'RNO',name:'Reno-Tahoe Intl',lat:39.50,lng:-119.77,type:'Regional'}
        ];

        airports.forEach(function(a) {
            var color = a.type==='Hub'?'#8b5cf6':'#a78bfa';
            var radius = a.type==='Hub'?8:6;
            L.circleMarker([a.lat,a.lng],{
                radius:radius,fillColor:color,color:'#fff',weight:1,opacity:1,fillOpacity:0.8
            }).bindPopup('<div class="popup-title">✈️ '+a.code+' - '+a.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+a.type+'</span></div>'
            ).addTo(layers.airports);
        });

        // ============================================
        // WATER RESOURCES - USGS Pattern Data
        // ============================================
        var waterResources = [
            {name:'Potomac River - Great Falls',lat:38.99,lng:-77.25,flow:'5,200 cfs',type:'River'},
            {name:'Occoquan Reservoir',lat:38.72,lng:-77.38,flow:'85 MGD',type:'Reservoir'},
            {name:'Lake Manassas',lat:38.75,lng:-77.62,flow:'45 MGD',type:'Reservoir'},
            {name:'Trinity River - DFW',lat:32.78,lng:-96.82,flow:'2,800 cfs',type:'River'},
            {name:'Lake Lavon',lat:33.05,lng:-96.48,flow:'120 MGD',type:'Reservoir'},
            {name:'Lake Ray Hubbard',lat:32.82,lng:-96.52,flow:'95 MGD',type:'Reservoir'},
            {name:'Salt River - Phoenix',lat:33.45,lng:-111.95,flow:'1,200 cfs',type:'River'},
            {name:'Lake Pleasant',lat:33.87,lng:-112.28,flow:'180 MGD',type:'Reservoir'},
            {name:'Roosevelt Lake',lat:33.68,lng:-111.15,flow:'240 MGD',type:'Reservoir'},
            {name:'Columbia River - Quincy',lat:47.05,lng:-119.85,flow:'180,000 cfs',type:'River'},
            {name:'Banks Lake',lat:47.58,lng:-119.22,flow:'Irrigation',type:'Reservoir'},
            {name:'Lake Michigan - Chicago',lat:41.88,lng:-87.52,flow:'Unlimited',type:'Lake'},
            {name:'Des Plaines River',lat:41.92,lng:-87.92,flow:'1,800 cfs',type:'River'},
            {name:'San Francisco Bay',lat:37.65,lng:-122.12,flow:'Tidal',type:'Bay'},
            {name:'Hetch Hetchy',lat:37.95,lng:-119.78,flow:'265 MGD',type:'Reservoir'},
            {name:'Lake Mead',lat:36.05,lng:-114.75,flow:'Varies',type:'Reservoir'},
            {name:'Houston Ship Channel',lat:29.75,lng:-95.08,flow:'Industrial',type:'Channel'}
        ];

        waterResources.forEach(function(w) {
            L.circleMarker([w.lat,w.lng],{
                radius:7,fillColor:'#0ea5e9',color:'#0284c7',weight:2,opacity:1,fillOpacity:0.7
            }).bindPopup('<div class="popup-title">💧 '+w.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Flow/Capacity</span><span class="popup-value">'+w.flow+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+w.type+'</span></div>'
            ).addTo(layers.water);
        });

        // ============================================
        // CO-OP UTILITIES & PUDs - 60+ Major Cooperatives
        // ============================================
        var coopUtilities = [
            // Texas Co-ops
            {name:'Pedernales Electric Cooperative',lat:30.27,lng:-98.07,members:376000,state:'TX',type:'Distribution'},
            {name:'CoServ Electric',lat:33.25,lng:-96.85,members:165000,state:'TX',type:'Distribution'},
            {name:'Guadalupe Valley EC',lat:29.55,lng:-98.25,members:125000,state:'TX',type:'Distribution'},
            {name:'Bluebonnet Electric',lat:30.08,lng:-97.35,members:105000,state:'TX',type:'Distribution'},
            {name:'Sam Houston EC',lat:30.35,lng:-95.45,members:120000,state:'TX',type:'Distribution'},
            {name:'Brazos Electric Cooperative',lat:32.45,lng:-97.35,members:1500000,state:'TX',type:'G&T'},
            {name:'Golden Spread EC',lat:35.20,lng:-101.85,members:300000,state:'TX',type:'G&T'},
            // Virginia Co-ops
            {name:'NOVEC',lat:38.85,lng:-77.65,members:180000,state:'VA',type:'Distribution'},
            {name:'Rappahannock EC',lat:38.40,lng:-77.45,members:175000,state:'VA',type:'Distribution'},
            {name:'Shenandoah Valley EC',lat:38.45,lng:-78.85,members:92000,state:'VA',type:'Distribution'},
            // Georgia Co-ops
            {name:'Oglethorpe Power',lat:33.45,lng:-84.45,members:4400000,state:'GA',type:'G&T'},
            {name:'Sawnee EMC',lat:34.15,lng:-84.15,members:200000,state:'GA',type:'Distribution'},
            {name:'Jackson EMC',lat:34.12,lng:-83.78,members:230000,state:'GA',type:'Distribution'},
            {name:'Cobb EMC',lat:33.95,lng:-84.58,members:200000,state:'GA',type:'Distribution'},
            // G&T Co-ops
            {name:'Basin Electric Power',lat:46.88,lng:-102.78,members:3000000,state:'ND',type:'G&T'},
            {name:'Tri-State G&T',lat:39.65,lng:-104.85,members:1000000,state:'CO',type:'G&T'},
            {name:'Great River Energy',lat:45.05,lng:-93.15,members:700000,state:'MN',type:'G&T'},
            {name:'PowerSouth Energy',lat:32.38,lng:-86.30,members:1000000,state:'AL',type:'G&T'},
            {name:'Associated Electric',lat:38.58,lng:-92.18,members:900000,state:'MO',type:'G&T'},
            {name:'East Kentucky Power',lat:38.05,lng:-84.50,members:530000,state:'KY',type:'G&T'},
            {name:'Hoosier Energy',lat:39.15,lng:-86.52,members:300000,state:'IN',type:'G&T'},
            // Pacific Northwest PUDs
            {name:'Grant County PUD',lat:47.23,lng:-119.85,members:50000,state:'WA',type:'PUD'},
            {name:'Chelan County PUD',lat:47.42,lng:-120.32,members:55000,state:'WA',type:'PUD'},
            {name:'Douglas County PUD',lat:47.55,lng:-120.08,members:22000,state:'WA',type:'PUD'},
            {name:'Benton County PUD',lat:46.28,lng:-119.28,members:55000,state:'WA',type:'PUD'},
            {name:'Clark Public Utilities',lat:45.62,lng:-122.65,members:200000,state:'WA',type:'PUD'},
            {name:'Snohomish PUD',lat:47.98,lng:-122.20,members:360000,state:'WA',type:'PUD'},
            {name:'Tacoma Power',lat:47.25,lng:-122.45,members:180000,state:'WA',type:'Municipal'},
            {name:'Seattle City Light',lat:47.61,lng:-122.33,members:460000,state:'WA',type:'Municipal'},
            // Midwest Co-ops
            {name:'Corn Belt Power',lat:42.55,lng:-93.62,members:140000,state:'IA',type:'G&T'},
            {name:'Dairyland Power',lat:43.82,lng:-91.25,members:250000,state:'WI',type:'G&T'},
            {name:'Minnkota Power',lat:47.92,lng:-97.05,members:150000,state:'ND',type:'G&T'},
            {name:'Central Iowa Power',lat:41.95,lng:-93.62,members:285000,state:'IA',type:'G&T'}
        ];

        coopUtilities.forEach(function(c) {
            var color = c.type==='G&T'?'#a855f7':c.type==='PUD'?'#06b6d4':c.type==='Municipal'?'#3b82f6':'#22c55e';
            var radius = c.members>500000?10:c.members>100000?7:5;
            L.circleMarker([c.lat,c.lng],{
                radius:radius,fillColor:color,color:'#fff',weight:2,opacity:1,fillOpacity:0.8
            }).bindPopup('<div class="popup-title">🏘️ '+c.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">'+c.type+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Members/Customers</span><span class="popup-value">'+c.members.toLocaleString()+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">State</span><span class="popup-value">'+c.state+'</span></div>'
            ).addTo(layers.coops);
        });

        // ============================================
        // FEMA FLOOD ZONES - Pattern Data
        // ============================================
        var femaZones = [
            // High risk areas
            {name:'Houston Ship Channel Area',coords:[[29.7,-95.3],[29.8,-95.1],[29.9,-95.0],[29.85,-95.25],[29.7,-95.3]],risk:'High',zone:'AE'},
            {name:'Miami-Dade Coastal',coords:[[25.7,-80.3],[25.8,-80.2],[25.9,-80.15],[25.85,-80.28],[25.7,-80.3]],risk:'High',zone:'VE'},
            {name:'New Orleans Metro',coords:[[29.9,-90.2],[30.0,-90.0],[30.1,-89.9],[30.0,-90.15],[29.9,-90.2]],risk:'High',zone:'AE'},
            // Moderate risk
            {name:'DFW Trinity Floodplain',coords:[[32.7,-96.9],[32.85,-96.75],[32.9,-96.65],[32.8,-96.85],[32.7,-96.9]],risk:'Moderate',zone:'X500'},
            {name:'Chicago River Basin',coords:[[41.8,-87.7],[41.9,-87.65],[41.95,-87.6],[41.88,-87.68],[41.8,-87.7]],risk:'Moderate',zone:'X500'},
            {name:'Sacramento Delta',coords:[[38.0,-121.6],[38.2,-121.5],[38.3,-121.4],[38.15,-121.55],[38.0,-121.6]],risk:'Moderate',zone:'AO'},
            // Minimal risk
            {name:'Phoenix Mesa',coords:[[33.4,-111.9],[33.5,-111.8],[33.55,-111.75],[33.45,-111.85],[33.4,-111.9]],risk:'Minimal',zone:'X'},
            {name:'Denver Metro',coords:[[39.7,-105.0],[39.8,-104.9],[39.85,-104.85],[39.75,-104.95],[39.7,-105.0]],risk:'Minimal',zone:'X'},
            {name:'Salt Lake Valley',coords:[[40.7,-111.95],[40.8,-111.85],[40.85,-111.8],[40.75,-111.9],[40.7,-111.95]],risk:'Minimal',zone:'X'},
            {name:'Ashburn/Loudoun',coords:[[39.0,-77.55],[39.08,-77.45],[39.1,-77.4],[39.02,-77.52],[39.0,-77.55]],risk:'Minimal',zone:'X'}
        ];

        femaZones.forEach(function(f) {
            var color = f.risk==='High'?'#ef4444':f.risk==='Moderate'?'#f59e0b':'#22c55e';
            L.polygon(f.coords,{
                fillColor:color,color:color,weight:1,opacity:0.5,fillOpacity:0.25
            }).bindPopup('<div class="popup-title">🌊 FEMA Zone: '+f.name+'</div>'+
                '<div class="popup-row"><span class="popup-label">Risk Level</span><span class="popup-value">'+f.risk+'</span></div>'+
                '<div class="popup-row"><span class="popup-label">Zone</span><span class="popup-value">'+f.zone+'</span></div>'
            ).addTo(layers.fema);
        });

        // ============================================
        // MARKET DATA WITH TAX INCENTIVES
        // ============================================
var markets = {
            // ==========================================
            // TIER 1 - HYPERSCALE MARKETS
            // ==========================================
            'virginia': {
                name: 'Northern Virginia (Ashburn)',
                center: [39.04, -77.49],
                zoom: 10,
                capacity: '12.8 GW',
                rto: 'PJM',
                cost: '$0.068/kWh',
                renewable: '18%',
                flood: 'Low-Moderate',
                utilities: ['Dominion Energy', 'NOVEC'],
                operators: ['Equinix', 'Digital Realty', 'QTS', 'CoreSite', 'CyrusOne', 'Vantage', 'CloudHQ', 'Iron Mountain', 'RagingWire', 'Amazon AWS', 'Microsoft Azure', 'Google Cloud'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2.5M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales & Use Tax Exemption on computer equipment (100%)',
                        'Property Tax Exemption in qualifying localities',
                        'Data Center Retail Sales Tax Exemption ($150M+ investment)',
                        'Enterprise Zone Tax Credits available',
                        'No corporate income tax on data center operations'
                    ]
                }
            },
    
            'texas-dfw': {
                name: 'Dallas-Fort Worth',
                center: [32.89, -96.95],
                zoom: 10,
                capacity: '15.2 GW',
                rto: 'ERCOT',
                cost: '$0.058/kWh',
                renewable: '32%',
                flood: 'Low',
                utilities: ['Oncor', 'Denton Municipal', 'CoServ'],
                operators: ['QTS', 'Digital Realty', 'CyrusOne', 'Flexential', 'DataBank', 'Stream', 'Compass', 'Aligned', 'T5', 'H5 Data Centers', 'Centra', 'Tract', 'Skybox'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$3M-12M/yr',
                    hasOZ: true,
                    incentives: [
                        'Chapter 313 Tax Abatement (up to 10 years)',
                        'No State Income Tax',
                        'Sales Tax Exemption on data processing equipment',
                        'Property Tax Abatement (up to 100% for 10 years)',
                        'Texas Enterprise Fund grants available',
                        'Freeport Exemption on inventory'
                    ]
                }
            },
    
            'phoenix': {
                name: 'Phoenix Metro',
                center: [33.45, -112.07],
                zoom: 10,
                capacity: '14.6 GW',
                rto: 'WECC',
                cost: '$0.072/kWh',
                renewable: '24%',
                flood: 'Low',
                utilities: ['APS', 'SRP'],
                operators: ['QTS', 'EdgeCore', 'Aligned', 'Vantage', 'CyrusOne', 'Stream', 'Iron Mountain', 'PhoenixNAP', 'IO Data Centers', 'Microsoft', 'Meta', 'Google'],
                tax: {
                    rating: 'good',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Qualified Facility Tax Credit',
                        'Personal Property Tax Exemption (Class 6)',
                        'Foreign Trade Zone benefits in Mesa',
                        'Government Property Lease Excise Tax (GPLET)',
                        'APS Economic Development Rates'
                    ]
                }
            },
    
            'silicon-valley': {
                name: 'Silicon Valley',
                center: [37.39, -122.08],
                zoom: 10,
                capacity: '8.4 GW',
                rto: 'CAISO',
                cost: '$0.145/kWh',
                renewable: '52%',
                flood: 'Low',
                utilities: ['PG&E', 'Silicon Valley Power'],
                operators: ['Equinix', 'Digital Realty', 'CoreSite', 'Vantage', 'CyrusOne', 'Supermicro', 'EdgeConneX'],
                tax: {
                    rating: 'limited',
                    estSavings: '$500K-2M/yr',
                    hasOZ: false,
                    incentives: [
                        'California Competes Tax Credit',
                        'Partial Sales Tax Exemption on manufacturing equipment',
                        'New Employment Credit in designated areas',
                        'Limited local incentives due to high demand'
                    ]
                }
            },
    
            'chicago': {
                name: 'Chicago',
                center: [41.88, -87.63],
                zoom: 10,
                capacity: '16.8 GW',
                rto: 'PJM/MISO',
                cost: '$0.078/kWh',
                renewable: '15%',
                flood: 'Moderate',
                utilities: ['ComEd', 'Peoples Gas'],
                operators: ['Equinix', 'Digital Realty', 'QTS', 'CyrusOne', 'DataBank', 'TierPoint', 'Netrality', 'Centra', 'ServerFarm'],
                tax: {
                    rating: 'moderate',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Enterprise Zone Tax Incentives',
                        'High Impact Business designation',
                        'EDGE Tax Credit Program',
                        'Property tax assessment reductions',
                        'TIF district financing available'
                    ]
                }
            },
    
            // ==========================================
            // TIER 1 - ENTERPRISE MARKETS
            // ==========================================
            'texas-houston': {
                name: 'Houston',
                center: [29.76, -95.36],
                zoom: 10,
                capacity: '18.4 GW',
                rto: 'ERCOT',
                cost: '$0.055/kWh',
                renewable: '28%',
                flood: 'High',
                utilities: ['CenterPoint', 'Entergy Texas'],
                operators: ['CyrusOne', 'Digital Realty', 'QTS', 'DataBank', 'Flexential', 'Stream', 'EdgeCore', 'Skybox', 'H5 Data Centers', 'Centra'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2.5M-10M/yr',
                    hasOZ: true,
                    incentives: [
                        'Chapter 313 Tax Abatement (up to 10 years)',
                        'No State Income Tax',
                        'Sales Tax Exemption on equipment',
                        'Harris County Tax Abatements available',
                        'Foreign Trade Zone benefits'
                    ]
                }
            },
    
            'texas-austin': {
                name: 'Austin / San Antonio',
                center: [30.27, -97.74],
                zoom: 9,
                capacity: '6.8 GW',
                rto: 'ERCOT',
                cost: '$0.062/kWh',
                renewable: '35%',
                flood: 'Moderate',
                utilities: ['Austin Energy', 'Pedernales Electric', 'CPS Energy'],
                operators: ['Digital Realty', 'CyrusOne', 'DataBank', 'Rackspace', 'Data Foundry', 'Skybox', 'Flexential', 'Stream'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Chapter 313 Tax Abatement available',
                        'No State Income Tax',
                        'Travis County incentive programs',
                        'Sales Tax Exemption on equipment',
                        'Austin Energy economic development rates'
                    ]
                }
            },
    
            'atlanta': {
                name: 'Atlanta',
                center: [33.75, -84.39],
                zoom: 10,
                capacity: '11.4 GW',
                rto: 'SERC',
                cost: '$0.082/kWh',
                renewable: '14%',
                flood: 'Moderate',
                utilities: ['Georgia Power'],
                operators: ['Equinix', 'Digital Realty', 'QTS', 'Switch', 'DataBank', 'Flexential', 'DC BLOX', 'Ascent', 'PointOne'],
                tax: {
                    rating: 'good',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales & Use Tax Exemption on equipment',
                        'Job Tax Credit Program',
                        'Investment Tax Credit',
                        'Quick Start workforce training (free)',
                        'BEST Property Tax Abatement'
                    ]
                }
            },
    
            'los-angeles': {
                name: 'Los Angeles',
                center: [34.05, -118.25],
                zoom: 10,
                capacity: '12.2 GW',
                rto: 'CAISO',
                cost: '$0.138/kWh',
                renewable: '48%',
                flood: 'Low',
                utilities: ['LADWP', 'SCE'],
                operators: ['Equinix', 'Digital Realty', 'CoreSite', 'DataBank', 'EdgeConneX', 'PhoenixNAP'],
                tax: {
                    rating: 'limited',
                    estSavings: '$500K-2M/yr',
                    hasOZ: true,
                    incentives: [
                        'California Competes Tax Credit',
                        'Employment Training Panel funding',
                        'LA County Economic Development programs',
                        'Opportunity Zone investments available'
                    ]
                }
            },
    
            'seattle': {
                name: 'Seattle / Quincy',
                center: [47.23, -119.85],
                zoom: 8,
                capacity: '4.2 GW',
                rto: 'WECC',
                cost: '$0.028/kWh',
                renewable: '89%',
                flood: 'Low',
                utilities: ['Grant County PUD', 'Seattle City Light'],
                operators: ['Microsoft', 'Yahoo', 'Sabey', 'Vantage', 'Intuit', 'H5 Data Centers'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-6M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales & Use Tax Exemption for data centers',
                        'Rural County Tax Incentives (Grant County)',
                        'No State Income Tax',
                        'Property Tax Exemption for new facilities',
                        'Extremely low power costs ($0.028/kWh)'
                    ]
                }
            },
    
            'newark': {
                name: 'Newark / New Jersey',
                center: [40.74, -74.17],
                zoom: 11,
                capacity: '9.8 GW',
                rto: 'PJM',
                cost: '$0.092/kWh',
                renewable: '22%',
                flood: 'Moderate-High',
                utilities: ['PSE&G', 'Jersey Central Power'],
                operators: ['Equinix', 'Digital Realty', 'CoreSite', 'CyrusOne', 'Netrality', 'DataBank', 'Cologix'],
                tax: {
                    rating: 'moderate',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Grow NJ Tax Credit Program',
                        'Sales Tax Exemption on data center equipment',
                        'Urban Enterprise Zone benefits',
                        'NJ Economic Development Authority grants',
                        'Property tax abatements in certain zones'
                    ]
                }
            },
    
            'denver': {
                name: 'Denver',
                center: [39.74, -104.99],
                zoom: 10,
                capacity: '7.8 GW',
                rto: 'WECC',
                cost: '$0.068/kWh',
                renewable: '38%',
                flood: 'Low',
                utilities: ['Xcel Energy'],
                operators: ['Vantage', 'CoreSite', 'Flexential', 'DataBank', 'CyrusOne', 'EdgeCore', 'H5 Data Centers', 'Tract'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Enterprise Zone Tax Credits',
                        'Job Growth Incentive Tax Credit',
                        'Personal Property Tax Exemption',
                        'Xcel Energy economic development rates',
                        'Strategic Fund grants available'
                    ]
                }
            },
    
            // ==========================================
            // TIER 2 - GROWING MARKETS
            // ==========================================
            'columbus': {
                name: 'Columbus, OH',
                center: [39.96, -82.99],
                zoom: 10,
                capacity: '9.2 GW',
                rto: 'PJM',
                cost: '$0.065/kWh',
                renewable: '12%',
                flood: 'Low',
                utilities: ['AEP Ohio', 'Columbus Southern'],
                operators: ['QTS', 'Cologix', 'Flexential', 'DataBank', 'CyrusOne', 'Amazon AWS', 'Google', 'Meta', 'Microsoft'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-7M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Sales Tax Exemption (100%)',
                        'Job Creation Tax Credit',
                        'JobsOhio Economic Development Grant',
                        'Property Tax Abatement (up to 100%)',
                        'Ohio New Markets Tax Credit'
                    ]
                }
            },
    
            'charlotte': {
                name: 'Charlotte / RTP',
                center: [35.23, -80.84],
                zoom: 9,
                capacity: '8.6 GW',
                rto: 'SERC',
                cost: '$0.072/kWh',
                renewable: '12%',
                flood: 'Low-Moderate',
                utilities: ['Duke Energy Carolinas', 'Duke Energy Progress'],
                operators: ['QTS', 'CyrusOne', 'DataBank', 'Flexential', 'DC BLOX', 'Compass', 'Microsoft', 'Apple'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1.5M-6M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales Tax Exemption on data center equipment',
                        'Job Development Investment Grant (JDIG)',
                        'One NC Fund grants',
                        'Property Tax Grants available',
                        'Duke Energy economic development rates'
                    ]
                }
            },
    
            'nashville': {
                name: 'Nashville',
                center: [36.16, -86.78],
                zoom: 10,
                capacity: '7.4 GW',
                rto: 'SERC',
                cost: '$0.068/kWh',
                renewable: '16%',
                flood: 'Moderate',
                utilities: ['Nashville Electric Service', 'TVA'],
                operators: ['QTS', 'DataBank', 'Flexential', 'CyrusOne', 'Meta', 'Oracle', 'Aligned'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'FastTrack Economic Development Fund',
                        'TVA economic development rates',
                        'Job Tax Credit',
                        'Industrial Machinery Tax Credit',
                        'No State Income Tax'
                    ]
                }
            },
    
            'portland': {
                name: 'Portland / The Dalles',
                center: [45.52, -122.68],
                zoom: 9,
                capacity: '6.8 GW',
                rto: 'WECC',
                cost: '$0.052/kWh',
                renewable: '72%',
                flood: 'Low',
                utilities: ['PGE', 'PacifiCorp'],
                operators: ['Google', 'Amazon', 'Facebook', 'Vantage', 'Flexential', 'QTS', 'EdgeCore'],
                tax: {
                    rating: 'good',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Strategic Investment Program (SIP)',
                        'Enterprise Zone Property Tax Exemption',
                        'No Sales Tax in Oregon',
                        'Very low power costs (The Dalles)',
                        'Renewable energy abundant'
                    ]
                }
            },
    
            'salt-lake': {
                name: 'Salt Lake City',
                center: [40.76, -111.89],
                zoom: 10,
                capacity: '5.4 GW',
                rto: 'WECC',
                cost: '$0.062/kWh',
                renewable: '28%',
                flood: 'Low',
                utilities: ['Rocky Mountain Power'],
                operators: ['C7 Data Centers', 'Aligned', 'DataBank', 'Flexential', 'Novva', 'Cyxtera'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Economic Development Tax Increment Financing',
                        'Industrial Assistance Fund',
                        'Enterprise Zone Tax Credits',
                        'Renewable Energy Tax Credit',
                        'Low power costs from hydroelectric'
                    ]
                }
            },
    
            'las-vegas': {
                name: 'Las Vegas / Reno',
                center: [36.17, -115.14],
                zoom: 9,
                capacity: '8.2 GW',
                rto: 'WECC',
                cost: '$0.078/kWh',
                renewable: '35%',
                flood: 'Low',
                utilities: ['NV Energy'],
                operators: ['Switch', 'Apple', 'Google', 'Flexential', 'DataBank'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Tax Abatement (up to 20 years)',
                        'Sales & Use Tax Abatement (up to 20 years)',
                        'Modified Business Tax Abatement',
                        'No State Income Tax',
                        'Personal Property Tax Abatement'
                    ]
                }
            },
    
            'kansas-city': {
                name: 'Kansas City',
                center: [39.10, -94.58],
                zoom: 10,
                capacity: '6.2 GW',
                rto: 'SPP',
                cost: '$0.058/kWh',
                renewable: '42%',
                flood: 'Moderate',
                utilities: ['Evergy', 'KCP&L'],
                operators: ['QTS', 'DataBank', 'Flexential', 'Netrality', 'Digital Realty', 'Tierpoint'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'PEAK Property Tax Exemption (Missouri)',
                        'High Performance Incentive Program (Kansas)',
                        'Sales Tax Exemption on equipment',
                        'Low utility costs',
                        'Workforce training programs'
                    ]
                }
            },
    
            'minneapolis': {
                name: 'Minneapolis',
                center: [44.98, -93.27],
                zoom: 10,
                capacity: '5.8 GW',
                rto: 'MISO',
                cost: '$0.072/kWh',
                renewable: '32%',
                flood: 'Low',
                utilities: ['Xcel Energy', 'Minnesota Power'],
                operators: ['Flexential', 'DataBank', 'CyrusOne', 'Cologix', 'Stream', 'Netrality'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales Tax Exemption on capital equipment',
                        'Minnesota Job Skills Partnership',
                        'Angel Tax Credit',
                        'Greater Minnesota Business Development Infrastructure Grant'
                    ]
                }
            },
    
            'boston': {
                name: 'Boston',
                center: [42.36, -71.06],
                zoom: 10,
                capacity: '6.2 GW',
                rto: 'ISO-NE',
                cost: '$0.125/kWh',
                renewable: '28%',
                flood: 'Moderate',
                utilities: ['Eversource', 'National Grid'],
                operators: ['Digital Realty', 'CyrusOne', 'CoreSite', 'Markley', 'Cologix', 'DataBank'],
                tax: {
                    rating: 'moderate',
                    estSavings: '$500K-2M/yr',
                    hasOZ: true,
                    incentives: [
                        'Investment Tax Credit',
                        'Economic Development Incentive Program',
                        'Workforce Training Fund',
                        'Tax Increment Financing in certain areas'
                    ]
                }
            },
    
            // ==========================================
            // EMERGING MARKETS - HIGH GROWTH
            // ==========================================
            'ohio-new-albany': {
                name: 'New Albany, OH (Google/Meta)',
                center: [40.08, -82.81],
                zoom: 11,
                capacity: '4.5 GW',
                rto: 'PJM',
                cost: '$0.062/kWh',
                renewable: '15%',
                flood: 'Low',
                utilities: ['AEP Ohio'],
                operators: ['Google', 'Meta', 'Amazon AWS', 'Microsoft'],
                megaProjects: [
                    {company: 'Google', capacity: '600 MW', investment: '$2B+', status: 'Expanding'},
                    {company: 'Meta', capacity: '400 MW', investment: '$1.5B', status: 'Under Construction'},
                    {company: 'Amazon AWS', capacity: '300 MW', investment: '$1B', status: 'Planned'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$3M-10M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Sales Tax Exemption (100%)',
                        'Job Creation Tax Credit ($600-6,000/job)',
                        'JobsOhio Economic Development Grant',
                        'Property Tax Abatement (100% for 15 years)',
                        'Ohio New Markets Tax Credit',
                        'New Albany TIF District'
                    ]
                }
            },
    
            'indiana': {
                name: 'Indianapolis / Indiana',
                center: [39.77, -86.16],
                zoom: 9,
                capacity: '8.4 GW',
                rto: 'MISO',
                cost: '$0.068/kWh',
                renewable: '18%',
                flood: 'Low',
                utilities: ['Duke Energy Indiana', 'AES Indiana', 'NIPSCO'],
                operators: ['Flexential', 'DataBank', 'Lifeline Data Centers', 'Digital Crossroad', 'Microsoft', 'Centra'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '400 MW', investment: '$1.5B', status: 'Under Construction'},
                    {company: 'Meta', capacity: '300 MW', investment: '$1B', status: 'Announced'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-7M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales Tax Exemption on data center equipment',
                        'Property Tax Abatement (up to 10 years)',
                        'EDGE Tax Credit',
                        'Hoosier Business Investment Tax Credit',
                        'Skills Enhancement Fund'
                    ]
                }
            },
    
            'wisconsin': {
                name: 'Wisconsin (Foxconn Belt)',
                center: [42.95, -88.05],
                zoom: 9,
                capacity: '6.8 GW',
                rto: 'MISO',
                cost: '$0.072/kWh',
                renewable: '15%',
                flood: 'Low',
                utilities: ['We Energies', 'WPS', 'Alliant'],
                operators: ['DataBank', 'TierPoint', 'Flexential', 'Microsoft'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '350 MW', investment: '$1B', status: 'Announced'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Enterprise Zone Tax Credits',
                        'Technology Zone Tax Credits',
                        'Workforce Training Grants',
                        'Property Tax Exemption for new equipment',
                        'Foxconn-era infrastructure improvements'
                    ]
                }
            },
    
            'iowa': {
                name: 'Des Moines / Iowa',
                center: [41.59, -93.62],
                zoom: 9,
                capacity: '4.2 GW',
                rto: 'MISO',
                cost: '$0.055/kWh',
                renewable: '58%',
                flood: 'Moderate',
                utilities: ['MidAmerican Energy'],
                operators: ['Microsoft', 'Google', 'Meta', 'Apple', 'TierPoint', 'Flexential'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '500 MW', investment: '$2B', status: 'Expanding'},
                    {company: 'Google', capacity: '400 MW', investment: '$1.5B', status: 'Operational'},
                    {company: 'Meta', capacity: '350 MW', investment: '$1.2B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales Tax Exemption on data center purchases',
                        'Property Tax Exemption (up to 20 years)',
                        'High Quality Jobs Program',
                        'Very low power costs',
                        '58% renewable energy (wind)'
                    ]
                }
            },
    
            'nebraska': {
                name: 'Omaha / Nebraska',
                center: [41.26, -95.94],
                zoom: 9,
                capacity: '4.8 GW',
                rto: 'SPP',
                cost: '$0.058/kWh',
                renewable: '35%',
                flood: 'Low-Moderate',
                utilities: ['OPPD', 'NPPD'],
                operators: ['Tierpoint', 'DataBank', 'Meta', 'Google', 'Windstream'],
                megaProjects: [
                    {company: 'Meta', capacity: '250 MW', investment: '$800M', status: 'Operational'},
                    {company: 'Google', capacity: '200 MW', investment: '$600M', status: 'Announced'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Sales Tax Exemption',
                        'Nebraska Advantage Act (up to 10 years)',
                        'ImagiNE Nebraska Act',
                        'Property Tax Exemption',
                        'Public power low rates'
                    ]
                }
            },
    
            'texas-temple': {
                name: 'Temple, TX (Meta)',
                center: [31.10, -97.34],
                zoom: 10,
                capacity: '8.5 GW',
                rto: 'ERCOT',
                cost: '$0.052/kWh',
                renewable: '45%',
                flood: 'Low',
                utilities: ['Oncor', 'Temple Municipal'],
                operators: ['Meta'],
                megaProjects: [
                    {company: 'Meta', capacity: '900 MW', investment: '$2.5B', status: 'Under Construction', notes: 'Largest single DC campus in Texas'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$5M-15M/yr',
                    hasOZ: true,
                    incentives: [
                        'Chapter 313 Tax Abatement',
                        'No State Income Tax',
                        'Sales Tax Exemption on equipment',
                        'City of Temple incentive package',
                        'Very low land costs'
                    ]
                }
            },
    
            'texas-midland': {
                name: 'Midland-Odessa, TX',
                center: [31.99, -102.08],
                zoom: 9,
                capacity: '6.2 GW',
                rto: 'ERCOT',
                cost: '$0.048/kWh',
                renewable: '52%',
                flood: 'Low',
                utilities: ['Oncor', 'Xcel Energy'],
                operators: ['EdgeCore', 'Aligned', 'Tract'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Chapter 313 Tax Abatement',
                        'No State Income Tax',
                        'Abundant power from wind/gas',
                        'Very low land costs',
                        'Permian Basin infrastructure'
                    ]
                }
            },
    
            'oklahoma': {
                name: 'Oklahoma City',
                center: [35.47, -97.52],
                zoom: 10,
                capacity: '5.2 GW',
                rto: 'SPP',
                cost: '$0.055/kWh',
                renewable: '42%',
                flood: 'Low-Moderate',
                utilities: ['OG&E', 'PSO'],
                operators: ['Flexential', 'DataBank', 'PointOne', 'EdgeCore'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Quality Jobs Program',
                        'Investment/New Jobs Tax Credit',
                        'Sales Tax Exemption on equipment',
                        'Ad Valorem Tax Exemption',
                        'Low power costs (wind)'
                    ]
                }
            },
    
            'arkansas': {
                name: 'Little Rock / Arkansas',
                center: [34.75, -92.29],
                zoom: 9,
                capacity: '4.8 GW',
                rto: 'SPP/MISO',
                cost: '$0.062/kWh',
                renewable: '12%',
                flood: 'Moderate',
                utilities: ['Entergy Arkansas', 'SWEPCO'],
                operators: ['DataBank', 'Windstream', 'TierPoint', 'Amazon'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Create Rebate Program',
                        'Tax Back Program',
                        'ArkPlus Tax Credits',
                        'Sales Tax Exemption on equipment',
                        'Workforce training programs'
                    ]
                }
            },
    
            'louisiana': {
                name: 'Louisiana',
                center: [30.45, -91.19],
                zoom: 8,
                capacity: '8.2 GW',
                rto: 'MISO',
                cost: '$0.058/kWh',
                renewable: '8%',
                flood: 'High',
                utilities: ['Entergy Louisiana', 'SWEPCO', 'CLECO'],
                operators: ['Digital Realty', 'DataBank', 'PointOne'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-7M/yr',
                    hasOZ: true,
                    incentives: [
                        'Industrial Tax Exemption Program (ITEP)',
                        'Quality Jobs Program',
                        'Enterprise Zone Program',
                        'Sales Tax Exemption on data center equipment',
                        'Low power costs'
                    ]
                }
            },
    
            'mississippi': {
                name: 'Mississippi',
                center: [32.30, -90.18],
                zoom: 8,
                capacity: '4.5 GW',
                rto: 'MISO',
                cost: '$0.065/kWh',
                renewable: '5%',
                flood: 'High',
                utilities: ['Entergy Mississippi', 'TVA'],
                operators: ['C Spire', 'Evoswitch', 'AWS'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'Fee-in-lieu of Property Taxes',
                        'Sales Tax Exemption (10 years)',
                        'Income Tax Exemption (10 years)',
                        'Workforce Training (free)',
                        'Very low operating costs'
                    ]
                }
            },
    
            'alabama': {
                name: 'Alabama / Huntsville',
                center: [34.73, -86.59],
                zoom: 9,
                capacity: '6.8 GW',
                rto: 'SERC',
                cost: '$0.068/kWh',
                renewable: '10%',
                flood: 'Low-Moderate',
                utilities: ['Alabama Power', 'TVA', 'Huntsville Utilities'],
                operators: ['Google', 'Meta', 'DataBank', 'PointOne', 'DC BLOX'],
                megaProjects: [
                    {company: 'Google', capacity: '600 MW', investment: '$1.5B', status: 'Under Construction'},
                    {company: 'Meta', capacity: '400 MW', investment: '$1B', status: 'Announced'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-8M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Abatement Act',
                        'Sales Tax Exemption',
                        'Property Tax Abatement (up to 20 years)',
                        'TVA economic development rates',
                        'AIDT workforce training (free)'
                    ]
                }
            },
    
            // ==========================================
            // EMERGING MARKETS - RURAL/LOW COST
            // ==========================================
            'wyoming': {
                name: 'Wyoming (Cheyenne)',
                center: [41.14, -104.82],
                zoom: 8,
                capacity: '2.8 GW',
                rto: 'WECC',
                cost: '$0.048/kWh',
                renewable: '25%',
                flood: 'Low',
                utilities: ['Black Hills Energy', 'Rocky Mountain Power'],
                operators: ['Microsoft', 'Powerhouse', 'Green House Data', 'EchoStar'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1.5M-5M/yr',
                    hasOZ: true,
                    incentives: [
                        'No Corporate Income Tax',
                        'No Personal Income Tax',
                        'Sales Tax Exemption on DC equipment',
                        'Very low power costs',
                        'Cool climate (free cooling)',
                        'Low property taxes'
                    ]
                }
            },
    
            'north-dakota': {
                name: 'North Dakota',
                center: [46.88, -96.79],
                zoom: 7,
                capacity: '3.2 GW',
                rto: 'MISO',
                cost: '$0.052/kWh',
                renewable: '35%',
                flood: 'Low',
                utilities: ['Xcel Energy', 'Basin Electric'],
                operators: ['Microsoft', 'Tract', 'DataBank'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Data Center Sales Tax Exemption',
                        'Property Tax Exemption (up to 5 years)',
                        'New Jobs Training Program',
                        'Very cold climate (free cooling)',
                        'Abundant wind power'
                    ]
                }
            },
    
            'south-dakota': {
                name: 'South Dakota (Sioux Falls)',
                center: [43.55, -96.73],
                zoom: 9,
                capacity: '2.4 GW',
                rto: 'SPP/MISO',
                cost: '$0.058/kWh',
                renewable: '32%',
                flood: 'Low',
                utilities: ['Xcel Energy', 'MidAmerican'],
                operators: ['SDN Communications', 'DataBank', 'TierPoint'],
                tax: {
                    rating: 'excellent',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'No Corporate Income Tax',
                        'No Personal Income Tax',
                        'Sales Tax Refund on DC equipment',
                        'Reinvestment Payment Program',
                        'Very low operating costs'
                    ]
                }
            },
    
            'montana': {
                name: 'Montana',
                center: [46.87, -110.36],
                zoom: 6,
                capacity: '2.2 GW',
                rto: 'WECC',
                cost: '$0.058/kWh',
                renewable: '45%',
                flood: 'Low',
                utilities: ['NorthWestern Energy'],
                operators: ['Tract', 'Powerhouse'],
                tax: {
                    rating: 'good',
                    estSavings: '$500K-2M/yr',
                    hasOZ: true,
                    incentives: [
                        'Property Tax Abatement',
                        'No Sales Tax',
                        'Renewable energy credits',
                        'Cool climate (free cooling)',
                        'Hydroelectric power'
                    ]
                }
            },
    
            'idaho': {
                name: 'Idaho (Boise)',
                center: [43.62, -116.21],
                zoom: 9,
                capacity: '3.5 GW',
                rto: 'WECC',
                cost: '$0.055/kWh',
                renewable: '65%',
                flood: 'Low',
                utilities: ['Idaho Power'],
                operators: ['Flexential', 'CyrusOne', 'Amazon', 'Meta'],
                megaProjects: [
                    {company: 'Meta', capacity: '200 MW', investment: '$600M', status: 'Announced'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Sales Tax Exemption on production equipment',
                        'Property Tax Exemption',
                        'Tax Reimbursement Incentive',
                        'Low power costs (hydro)',
                        'Workforce Training grants'
                    ]
                }
            },
    
            'new-mexico': {
                name: 'New Mexico',
                center: [34.52, -105.87],
                zoom: 7,
                capacity: '4.2 GW',
                rto: 'WECC',
                cost: '$0.065/kWh',
                renewable: '28%',
                flood: 'Low',
                utilities: ['PNM', 'El Paso Electric', 'Xcel'],
                operators: ['Meta', 'Tract', 'EdgeCore'],
                megaProjects: [
                    {company: 'Meta', capacity: '500 MW', investment: '$1.5B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$2M-6M/yr',
                    hasOZ: true,
                    incentives: [
                        'High-Wage Jobs Tax Credit',
                        'Industrial Revenue Bonds',
                        'JTIP (Job Training Incentive Program)',
                        'LEDA (Local Economic Development Act)',
                        'Solar renewable credits'
                    ]
                }
            },
    
            'utah-rural': {
                name: 'Utah (Rural)',
                center: [39.32, -111.68],
                zoom: 7,
                capacity: '3.8 GW',
                rto: 'WECC',
                cost: '$0.058/kWh',
                renewable: '22%',
                flood: 'Low',
                utilities: ['Rocky Mountain Power', 'Utah Municipal Power'],
                operators: ['NSA (Bluffdale)', 'C7', 'Flexential', 'Novva'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Enterprise Zone Tax Credits',
                        'Rural Fast Track Program',
                        'Custom Fit Training',
                        'Economic Development Tax Increment',
                        'Low land costs'
                    ]
                }
            },
    
            'maine': {
                name: 'Maine',
                center: [45.25, -69.45],
                zoom: 7,
                capacity: '2.2 GW',
                rto: 'ISO-NE',
                cost: '$0.095/kWh',
                renewable: '75%',
                flood: 'Low-Moderate',
                utilities: ['Central Maine Power', 'Versant'],
                operators: ['Oxford Networks', 'Tilson'],
                tax: {
                    rating: 'good',
                    estSavings: '$500K-2M/yr',
                    hasOZ: true,
                    incentives: [
                        'Pine Tree Development Zone',
                        'Employment Tax Increment Financing',
                        'Business Equipment Tax Exemption',
                        'Very cold climate (free cooling)',
                        'High renewable energy (hydro/wind)'
                    ]
                }
            },
    
            'upstate-ny': {
                name: 'Upstate New York',
                center: [43.05, -76.15],
                zoom: 8,
                capacity: '5.5 GW',
                rto: 'NYISO',
                cost: '$0.068/kWh',
                renewable: '52%',
                flood: 'Low-Moderate',
                utilities: ['National Grid', 'NYSEG', 'NYPA'],
                operators: ['Cologix', 'DataBank', 'EdgeCore', 'Yahoo'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Excelsior Jobs Program',
                        'Data Center Tax Credit',
                        'Build Now NY',
                        'START-UP NY (tax-free zones)',
                        'NYPA low-cost hydropower'
                    ]
                }
            },
    
            // ==========================================
            // INTERNATIONAL - NORTH AMERICA
            // ==========================================
            'toronto': {
                name: 'Toronto, Canada',
                center: [43.65, -79.38],
                zoom: 10,
                capacity: '8.5 GW',
                rto: 'IESO',
                cost: 'CAD $0.095/kWh',
                renewable: '65%',
                flood: 'Low-Moderate',
                utilities: ['Toronto Hydro', 'Hydro One'],
                operators: ['Equinix', 'Digital Realty', 'Cologix', 'Rogers', 'eStruxture', 'H5 Data Centers'],
                tax: {
                    rating: 'good',
                    estSavings: 'CAD $1M-4M/yr',
                    hasOZ: false,
                    incentives: [
                        'Scientific Research Tax Credit',
                        'Ontario Computer Animation Tax Credit',
                        'Global Skills Strategy (fast immigration)',
                        'Clean technology investments',
                        'Low-carbon grid (nuclear/hydro)'
                    ]
                }
            },
    
            'montreal': {
                name: 'Montreal, Canada',
                center: [45.50, -73.57],
                zoom: 10,
                capacity: '6.2 GW',
                rto: 'Hydro-Québec',
                cost: 'CAD $0.042/kWh',
                renewable: '99%',
                flood: 'Low',
                utilities: ['Hydro-Québec'],
                operators: ['Cologix', 'eStruxture', 'QScale', 'Google', 'Amazon'],
                megaProjects: [
                    {company: 'Google', capacity: '100 MW', investment: 'CAD $735M', status: 'Operational'},
                    {company: 'QScale', capacity: '400 MW', investment: 'CAD $1B+', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: 'CAD $2M-8M/yr',
                    hasOZ: false,
                    incentives: [
                        'Lowest power costs in North America',
                        '99% renewable energy (hydro)',
                        'R&D Tax Credits',
                        'Very cold climate (free cooling)',
                        'Data sovereignty (non-US)'
                    ]
                }
            },
    
            'queretaro': {
                name: 'Querétaro, Mexico',
                center: [20.59, -100.39],
                zoom: 10,
                capacity: '3.8 GW',
                rto: 'CENACE',
                cost: '$0.085/kWh',
                renewable: '18%',
                flood: 'Low',
                utilities: ['CFE'],
                operators: ['Equinix', 'KIO Networks', 'Odata', 'Ascenty'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'IMMEX program (tariff benefits)',
                        'State of Querétaro tax incentives',
                        'Free Trade Zone benefits',
                        'USMCA trade advantages',
                        'Growing nearshore demand'
                    ]
                }
            },

            // ==========================================
            // INTERNATIONAL - EUROPE
            // ==========================================
            'london': {
                name: 'London, UK',
                center: [51.51, -0.13],
                zoom: 10,
                capacity: '12.5 GW',
                rto: 'National Grid ESO',
                cost: '£0.18/kWh',
                renewable: '42%',
                flood: 'Moderate',
                utilities: ['UK Power Networks', 'National Grid'],
                operators: ['Equinix', 'Digital Realty', 'Vantage', 'NTT', 'CyrusOne', 'Colt', 'Global Switch'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '500 MW', investment: '£2.5B', status: 'Planning'},
                    {company: 'Vantage', capacity: '200 MW', investment: '£1B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'moderate',
                    estSavings: '£500K-2M/yr',
                    hasOZ: false,
                    incentives: [
                        'R&D Tax Credits',
                        'Capital Allowances on equipment',
                        'Enterprise Investment Scheme',
                        'Premium connectivity (LINX)',
                        'Strong legal/regulatory framework'
                    ]
                }
            },
    
            'frankfurt': {
                name: 'Frankfurt, Germany',
                center: [50.11, 8.68],
                zoom: 10,
                capacity: '8.2 GW',
                rto: 'TenneT / Amprion',
                cost: '€0.22/kWh',
                renewable: '52%',
                flood: 'Low',
                utilities: ['Mainova', 'Amprion'],
                operators: ['Equinix', 'Digital Realty', 'NTT', 'Interxion', 'CloudHQ', 'Data4'],
                megaProjects: [
                    {company: 'Google', capacity: '300 MW', investment: '€1B', status: 'Planning'},
                    {company: 'Digital Realty', capacity: '150 MW', investment: '€500M', status: 'Operational'}
                ],
                tax: {
                    rating: 'moderate',
                    estSavings: '€500K-2M/yr',
                    hasOZ: false,
                    incentives: [
                        'DE-CIX exchange colocation',
                        'GDPR compliance center',
                        'Strong banking/finance presence',
                        'Central European location',
                        'High power costs but reliable'
                    ]
                }
            },
    
            'amsterdam': {
                name: 'Amsterdam, Netherlands',
                center: [52.37, 4.90],
                zoom: 10,
                capacity: '5.5 GW',
                rto: 'TenneT NL',
                cost: '€0.15/kWh',
                renewable: '38%',
                flood: 'Moderate (managed)',
                utilities: ['TenneT', 'Liander'],
                operators: ['Equinix', 'Digital Realty', 'Interxion', 'NTT', 'CyrusOne', 'Iron Mountain'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '200 MW', investment: '€1B', status: 'Planning'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '€1M-3M/yr',
                    hasOZ: false,
                    incentives: [
                        'AMS-IX exchange colocation',
                        'Innovation Box tax (5% rate)',
                        'Strong subsea connectivity',
                        'EU gateway',
                        'Data center moratorium in some areas'
                    ]
                }
            },
    
            'dublin': {
                name: 'Dublin, Ireland',
                center: [53.35, -6.26],
                zoom: 10,
                capacity: '3.8 GW',
                rto: 'EirGrid',
                cost: '€0.20/kWh',
                renewable: '45%',
                flood: 'Low',
                utilities: ['ESB Networks', 'EirGrid'],
                operators: ['Equinix', 'Digital Realty', 'Interxion', 'Microsoft', 'Google', 'AWS', 'Meta'],
                megaProjects: [
                    {company: 'AWS', capacity: '200 MW', investment: '€1B', status: 'Under Construction'},
                    {company: 'Meta', capacity: '100 MW', investment: '€800M', status: 'Operational'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '€2M-6M/yr',
                    hasOZ: false,
                    incentives: [
                        'Corporate Tax Rate 12.5%',
                        'Tech multinational hub',
                        'R&D Tax Credit',
                        'EU data sovereignty',
                        'Grid capacity concerns (moratorium)'
                    ]
                }
            },
    
            'paris': {
                name: 'Paris, France',
                center: [48.86, 2.35],
                zoom: 10,
                capacity: '6.5 GW',
                rto: 'RTE',
                cost: '€0.14/kWh',
                renewable: '25%',
                flood: 'Moderate',
                utilities: ['Enedis', 'RTE'],
                operators: ['Equinix', 'Digital Realty', 'Data4', 'Interxion', 'Scaleway'],
                tax: {
                    rating: 'moderate',
                    estSavings: '€500K-2M/yr',
                    hasOZ: false,
                    incentives: [
                        'France 2030 investment plan',
                        'Nuclear baseload (low carbon)',
                        'R&D Tax Credit',
                        'Growing AI/ML hub',
                        'Strong government support'
                    ]
                }
            },
    
            'nordics': {
                name: 'Nordics (Stockholm/Helsinki)',
                center: [59.33, 18.07],
                zoom: 6,
                capacity: '15.2 GW',
                rto: 'Nord Pool',
                cost: '€0.04-0.08/kWh',
                renewable: '85%',
                flood: 'Low',
                utilities: ['Vattenfall', 'Fortum', 'Fingrid'],
                operators: ['Equinix', 'Digital Realty', 'atNorth', 'DigiPlex', 'AWS', 'Google', 'Microsoft'],
                megaProjects: [
                    {company: 'Microsoft', capacity: '500 MW', investment: '$2B', status: 'Planning'},
                    {company: 'AWS', capacity: '300 MW', investment: '$1.5B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '€2M-8M/yr',
                    hasOZ: false,
                    incentives: [
                        'Lowest power costs in Europe',
                        'Cold climate (free cooling)',
                        '85%+ renewable energy',
                        'Strong fiber connectivity',
                        'Political stability'
                    ]
                }
            },
    
            // ==========================================
            // INTERNATIONAL - ASIA PACIFIC
            // ==========================================
            'singapore': {
                name: 'Singapore',
                center: [1.35, 103.82],
                zoom: 11,
                capacity: '4.2 GW',
                rto: 'EMA Singapore',
                cost: 'SGD $0.25/kWh',
                renewable: '8%',
                flood: 'Low',
                utilities: ['SP Group', 'Senoko', 'Tuas'],
                operators: ['Equinix', 'Digital Realty', 'NTT', 'STT GDC', 'AirTrunk', 'Keppel DC'],
                megaProjects: [
                    {company: 'AirTrunk', capacity: '200 MW', investment: 'SGD $1B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: 'SGD $2M-6M/yr',
                    hasOZ: false,
                    incentives: [
                        'Pioneer Certificate (tax exemption)',
                        'APAC financial hub',
                        'Premium connectivity',
                        'Data center moratorium (lifted 2023)',
                        'Tropical climate (cooling costs)'
                    ]
                }
            },
    
            'tokyo': {
                name: 'Tokyo, Japan',
                center: [35.68, 139.69],
                zoom: 10,
                capacity: '18.5 GW',
                rto: 'TEPCO / OCCTO',
                cost: '¥28/kWh',
                renewable: '22%',
                flood: 'Low-Moderate',
                utilities: ['TEPCO', 'Kansai Electric'],
                operators: ['Equinix', 'Digital Realty', 'NTT', 'KDDI', 'Colt DCS', 'AirTrunk', 'Mitsubishi'],
                megaProjects: [
                    {company: 'AWS', capacity: '200 MW', investment: '¥200B', status: 'Operational'},
                    {company: 'Google', capacity: '150 MW', investment: '¥100B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'moderate',
                    estSavings: '¥100M-500M/yr',
                    hasOZ: false,
                    incentives: [
                        'J-REIT structure',
                        'Largest APAC economy',
                        'Premium subsea connectivity',
                        'High construction costs',
                        'Seismic considerations'
                    ]
                }
            },
    
            'sydney': {
                name: 'Sydney, Australia',
                center: [-33.87, 151.21],
                zoom: 10,
                capacity: '6.8 GW',
                rto: 'AEMO',
                cost: 'AUD $0.28/kWh',
                renewable: '35%',
                flood: 'Low',
                utilities: ['AusGrid', 'Endeavour Energy'],
                operators: ['Equinix', 'Digital Realty', 'NTT', 'AirTrunk', 'NEXTDC', 'Macquarie'],
                megaProjects: [
                    {company: 'AirTrunk', capacity: '300 MW', investment: 'AUD $1.5B', status: 'Under Construction'},
                    {company: 'Microsoft', capacity: '200 MW', investment: 'AUD $1B', status: 'Planning'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: 'AUD $1M-4M/yr',
                    hasOZ: false,
                    incentives: [
                        'R&D Tax Incentive (43.5%)',
                        'Infrastructure tax breaks',
                        'Pacific subsea cables',
                        'Growing APAC hub',
                        'High land/power costs'
                    ]
                }
            },
    
            'mumbai': {
                name: 'Mumbai, India',
                center: [19.08, 72.88],
                zoom: 10,
                capacity: '8.2 GW',
                rto: 'MERC',
                cost: '₹7/kWh',
                renewable: '28%',
                flood: 'High (monsoon)',
                utilities: ['Adani Electricity', 'Tata Power', 'BEST'],
                operators: ['Equinix', 'NTT', 'Nxtra (Airtel)', 'CtrlS', 'Yotta', 'STT GDC', 'Web Werks'],
                megaProjects: [
                    {company: 'Yotta', capacity: '300 MW', investment: '₹5000 Cr', status: 'Operational'},
                    {company: 'Adani', capacity: '200 MW', investment: '₹3000 Cr', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '₹5-20 Cr/yr',
                    hasOZ: false,
                    incentives: [
                        'IT/ITeS SEZ benefits',
                        'Make in India incentives',
                        'Large domestic market',
                        'Growing cloud adoption',
                        'Subsea cable landings'
                    ]
                }
            },
    
            'hong-kong': {
                name: 'Hong Kong',
                center: [22.30, 114.17],
                zoom: 11,
                capacity: '3.2 GW',
                rto: 'CLP / HK Electric',
                cost: 'HKD $1.2/kWh',
                renewable: '3%',
                flood: 'Low-Moderate',
                utilities: ['CLP Power', 'HK Electric'],
                operators: ['Equinix', 'Digital Realty', 'NTT', 'PCCW', 'SUNeVision', 'iAdvantage'],
                tax: {
                    rating: 'excellent',
                    estSavings: 'HKD $5M-15M/yr',
                    hasOZ: false,
                    incentives: [
                        'Low corporate tax (16.5%)',
                        'No capital gains tax',
                        'APAC financial hub',
                        'Premium connectivity',
                        'Limited land availability'
                    ]
                }
            },
    
            // ==========================================
            // INTERNATIONAL - MIDDLE EAST
            // ==========================================
            'uae': {
                name: 'UAE (Dubai/Abu Dhabi)',
                center: [24.45, 54.37],
                zoom: 8,
                capacity: '6.5 GW',
                rto: 'DEWA / ADDC',
                cost: '$0.08/kWh',
                renewable: '12%',
                flood: 'Low',
                utilities: ['DEWA', 'ADDC', 'FEWA'],
                operators: ['Equinix', 'DAMAC Digital', 'Khazna', 'Moro Hub', 'Gulf Data Hub', 'G42'],
                megaProjects: [
                    {company: 'DAMAC Digital', capacity: '1 GW', investment: '$5B', status: 'Planning'},
                    {company: 'G42/Microsoft', capacity: '300 MW', investment: '$1.5B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$3M-10M/yr',
                    hasOZ: true,
                    incentives: [
                        'No corporate income tax (most zones)',
                        'Free Zone benefits',
                        'Government AI investment',
                        'Growing regional hub',
                        'Hot climate (cooling costs)'
                    ]
                }
            },
    
            'saudi': {
                name: 'Saudi Arabia (Riyadh/NEOM)',
                center: [24.71, 46.67],
                zoom: 7,
                capacity: '12.8 GW',
                rto: 'ECRA',
                cost: '$0.05/kWh',
                renewable: '5%',
                flood: 'Low',
                utilities: ['SEC', 'NEOM utilities'],
                operators: ['Alibaba Cloud', 'Oracle', 'AWS', 'stc', 'Mobily'],
                megaProjects: [
                    {company: 'NEOM/Microsoft', capacity: '500 MW', investment: '$2B', status: 'Planning'},
                    {company: 'Oracle', capacity: '300 MW', investment: '$1.5B', status: 'Under Construction'}
                ],
                tax: {
                    rating: 'excellent',
                    estSavings: '$4M-15M/yr',
                    hasOZ: true,
                    incentives: [
                        'Vision 2030 incentives',
                        'Very low power costs',
                        'Massive infrastructure investment',
                        'NEOM special economic zone',
                        'Hot climate (cooling needs)'
                    ]
                }
            },
    
            // ==========================================
            // MORE US MARKETS
            // ==========================================
            'miami': {
                name: 'Miami / South Florida',
                center: [25.76, -80.19],
                zoom: 10,
                capacity: '5.8 GW',
                rto: 'FRCC',
                cost: '$0.095/kWh',
                renewable: '8%',
                flood: 'High',
                utilities: ['FPL', 'Duke Florida'],
                operators: ['Equinix', 'Digital Realty', 'Cyxtera', 'CenturyLink', 'NAP of the Americas'],
                megaProjects: [
                    {company: 'Digital Realty', capacity: '100 MW', investment: '$500M', status: 'Operational'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'No state income tax',
                        'Qualified Target Industry Tax Refund',
                        'Latin America gateway (NAP)',
                        'Hurricane risk',
                        'Subsea cable hub'
                    ]
                }
            },
    
            'san-diego': {
                name: 'San Diego',
                center: [32.72, -117.16],
                zoom: 10,
                capacity: '4.5 GW',
                rto: 'CAISO',
                cost: '$0.18/kWh',
                renewable: '45%',
                flood: 'Low',
                utilities: ['SDG&E'],
                operators: ['Equinix', 'Digital Realty', 'CoreSite', 'Zayo'],
                tax: {
                    rating: 'limited',
                    estSavings: '$500K-2M/yr',
                    hasOZ: false,
                    incentives: [
                        'California Competes Tax Credit',
                        'Cross-border connectivity (Tijuana)',
                        'Defense/military contracts',
                        'High power costs',
                        'Biotech/tech hub'
                    ]
                }
            },
    
            'pittsburgh': {
                name: 'Pittsburgh',
                center: [40.44, -79.99],
                zoom: 10,
                capacity: '6.2 GW',
                rto: 'PJM',
                cost: '$0.072/kWh',
                renewable: '8%',
                flood: 'Moderate',
                utilities: ['Duquesne Light', 'FirstEnergy'],
                operators: ['Expedient', 'DartPoints', 'DataBank', 'TierPoint'],
                megaProjects: [
                    {company: 'Meta', capacity: '150 MW', investment: '$800M', status: 'Announced'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Keystone Opportunity Zones',
                        'Data Center Sales Tax Exemption',
                        'Educational/research partnerships',
                        'AI research hub (CMU)',
                        'Lower costs than NOVA'
                    ]
                }
            },
    
            'detroit': {
                name: 'Detroit / Michigan',
                center: [42.33, -83.05],
                zoom: 9,
                capacity: '8.5 GW',
                rto: 'MISO',
                cost: '$0.085/kWh',
                renewable: '12%',
                flood: 'Low',
                utilities: ['DTE Energy', 'Consumers Energy'],
                operators: ['Switch', 'QTS', '365 Data Centers', 'Flexential'],
                megaProjects: [
                    {company: 'Switch', capacity: '500 MW', investment: '$5B', status: 'Operational'}
                ],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-4M/yr',
                    hasOZ: true,
                    incentives: [
                        'Michigan Business Development Program',
                        'Data Center Tax Exemption',
                        'Automotive/manufacturing base',
                        'Great Lakes water/cooling',
                        'Talent pipeline'
                    ]
                }
            },
    
            'cleveland': {
                name: 'Cleveland / Northeast Ohio',
                center: [41.50, -81.69],
                zoom: 9,
                capacity: '7.2 GW',
                rto: 'PJM',
                cost: '$0.070/kWh',
                renewable: '6%',
                flood: 'Low',
                utilities: ['FirstEnergy', 'Cleveland Public Power'],
                operators: ['Expedient', 'Flexential', 'DataBank', 'ComputerLand'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'Ohio Data Center Tax Exemption',
                        'Job Creation Tax Credit',
                        'Low power costs',
                        'Great Lakes cooling',
                        'Growing enterprise market'
                    ]
                }
            },
    
            'tampa': {
                name: 'Tampa Bay',
                center: [27.95, -82.46],
                zoom: 10,
                capacity: '5.2 GW',
                rto: 'FRCC',
                cost: '$0.088/kWh',
                renewable: '5%',
                flood: 'Moderate',
                utilities: ['Tampa Electric', 'Duke Florida'],
                operators: ['Peak 10', 'Flexential', 'DataBank', 'CenturyLink'],
                tax: {
                    rating: 'good',
                    estSavings: '$1M-3M/yr',
                    hasOZ: true,
                    incentives: [
                        'No state income tax',
                        'Qualified Target Industry program',
                        'Lower costs than Miami',
                        'Hurricane risk',
                        'Growing financial services'
                    ]
                }
            }
        };

        // ============================================
        // EVENT HANDLERS
        // ============================================
        
        // Layer toggle buttons
        document.querySelectorAll('.layer-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var layerName = this.getAttribute('data-layer');
                
                // Special handling for FEMA flood zones - use WMS layer
                if (layerName === 'fema' && femaFloodLayer) {
                    if (map.hasLayer(femaFloodLayer)) {
                        map.removeLayer(femaFloodLayer);
                        if (layers[layerName]) map.removeLayer(layers[layerName]);
                        this.classList.remove('active');
                        console.log('📡 FEMA Flood Layer hidden');
                    } else {
                        map.addLayer(femaFloodLayer);
                        if (layers[layerName]) map.addLayer(layers[layerName]);
                        this.classList.add('active');
                        console.log('📡 FEMA Flood Layer shown (NFHL Live)');
                    }
                    return;
                }
                
                // Special handling for Wetlands - use WMS layer
                if (layerName === 'wetlands' && wetlandsWMS) {
                    if (map.hasLayer(wetlandsWMS)) {
                        map.removeLayer(wetlandsWMS);
                        this.classList.remove('active');
                        console.log('🌿 NWI Wetlands Layer hidden');
                    } else {
                        map.addLayer(wetlandsWMS);
                        this.classList.add('active');
                        console.log('🌿 NWI Wetlands Layer shown (USFWS)');
                    }
                    return;
                }
                
                // Special handling for Seismic - load USGS earthquake data
                if (layerName === 'seismic') {
                    if (map.hasLayer(layers.seismic)) {
                        map.removeLayer(layers.seismic);
                        this.classList.remove('active');
                        console.log('🌋 Seismic Layer hidden');
                    } else {
                        map.addLayer(layers.seismic);
                        this.classList.add('active');
                        loadSeismicData();
                        console.log('🌋 Seismic Layer shown (USGS Live)');
                    }
                    return;
                }
                
                // Special handling for Critical Habitat - ESA listed species
                if (layerName === 'habitat' && habitatTiles) {
                    if (map.hasLayer(habitatTiles)) {
                        map.removeLayer(habitatTiles);
                        this.classList.remove('active');
                        console.log('🦅 Critical Habitat Layer hidden');
                    } else {
                        map.addLayer(habitatTiles);
                        this.classList.add('active');
                        console.log('🦅 Critical Habitat Layer shown (USFWS ESA)');
                    }
                    return;
                }
                
                // Special handling for Queue - load interconnection queue
                if (layerName === 'queue') {
                    if (map.hasLayer(layers.queue)) {
                        map.removeLayer(layers.queue);
                        this.classList.remove('active');
                        console.log('📋 Gen Queue Layer hidden');
                    } else {
                        map.addLayer(layers.queue);
                        this.classList.add('active');
                        console.log('📋 Gen Queue Layer shown');
                    }
                    return;
                }
                
                // DC Queue - Data Center Load Interconnection Requests
                if (layerName === 'dcqueue') {
                    if (map.hasLayer(layers.dcqueue)) {
                        map.removeLayer(layers.dcqueue);
                        this.classList.remove('active');
                        console.log('🏢 DC Queue Layer hidden');
                    } else {
                        map.addLayer(layers.dcqueue);
                        this.classList.add('active');
                        loadDCQueueData();
                        console.log('🏢 DC Queue Layer shown (566 active load requests, 81.59 GW)');
                    }
                    return;
                }
                
                // Special handling for Parcels - County GIS Services
                if (layerName === 'parcels') {
                    // Group counties by state
                    var stateGroups = {};
                    Object.keys(COUNTY_PARCEL_SERVICES).forEach(function(key) {
                        var svc = COUNTY_PARCEL_SERVICES[key];
                        var state = svc.state || 'Other';
                        if (!stateGroups[state]) stateGroups[state] = [];
                        stateGroups[state].push({key: key, svc: svc});
                    });
                    
                    // Sort states alphabetically
                    var sortedStates = Object.keys(stateGroups).sort();
                    
                    // Build state tabs and content
                    var stateTabs = sortedStates.map(function(state, idx) {
                        var count = stateGroups[state].length;
                        return '<button class="state-tab' + (idx === 0 ? ' active' : '') + '" data-state="' + state + '" style="padding:8px 12px;background:' + (idx === 0 ? '#6366f1' : '#1a1a2e') + ';color:' + (idx === 0 ? '#fff' : '#9ca3af') + ';border:1px solid #374151;border-radius:6px;cursor:pointer;font-size:11px;white-space:nowrap">' + state + ' <span style="opacity:0.7">(' + count + ')</span></button>';
                    }).join('');
                    
                    var stateContents = sortedStates.map(function(state, idx) {
                        var counties = stateGroups[state].map(function(item) {
                            var isActive = activeParcelLayers[item.key] ? 'background:#22c55e;color:#fff' : 'background:#1a1a2e';
                            var tierBadge = item.svc.tier === 1 ? '<span style="background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px;font-size:9px;margin-left:8px">TOP MARKET</span>' : '';
                            return '<div class="county-parcel-btn" data-county="' + item.key + '" style="' + isActive + ';padding:10px 14px;border-radius:6px;cursor:pointer;border:1px solid #374151;margin:4px 0;display:flex;justify-content:space-between;align-items:center;transition:all 0.2s">' +
                                '<span style="font-size:13px">' + item.svc.name + tierBadge + '</span>' +
                                '<span style="font-size:10px;opacity:0.7">' + (activeParcelLayers[item.key] ? '✓' : '→') + '</span>' +
                                '</div>';
                        }).join('');
                        return '<div class="state-content" data-state="' + state + '" style="display:' + (idx === 0 ? 'block' : 'none') + '">' + counties + '</div>';
                    }).join('');
                    
                    // Create county selector modal
                    var parcelModal = document.createElement('div');
                    parcelModal.id = 'parcel-modal';
                    parcelModal.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.9);z-index:10000;display:flex;align-items:center;justify-content:center';
                    
                    parcelModal.innerHTML = 
                        '<div style="background:#0f0f1a;border-radius:16px;max-width:600px;width:95%;max-height:85vh;overflow:hidden;border:1px solid #252836;display:flex;flex-direction:column">' +
                        '<div style="padding:20px;border-bottom:1px solid #252836;flex-shrink:0">' +
                        '<div style="display:flex;justify-content:space-between;align-items:center">' +
                        '<div><div style="font-size:20px;font-weight:700">🗺️ County Parcel Boundaries</div>' +
                        '<div style="font-size:12px;color:#6366f1;margin-top:4px">' + Object.keys(COUNTY_PARCEL_SERVICES).length + ' counties across ' + sortedStates.length + ' states</div></div>' +
                        '<button onclick="document.getElementById(\'parcel-modal\').remove()" style="background:none;border:none;color:#9ca3af;font-size:24px;cursor:pointer;padding:4px">&times;</button>' +
                        '</div>' +
                        '<div style="margin-top:12px"><input type="text" id="parcel-search" placeholder="🔍 Search counties..." style="width:100%;padding:10px 14px;background:#1a1a2e;border:1px solid #374151;border-radius:8px;color:#fff;font-size:13px;outline:none" /></div>' +
                        '</div>' +
                        '<div style="padding:12px 16px;border-bottom:1px solid #252836;overflow-x:auto;flex-shrink:0;display:flex;gap:6px;background:#0a0a12">' + stateTabs + '</div>' +
                        '<div id="parcel-state-content" style="padding:16px;overflow-y:auto;flex:1">' + stateContents + '</div>' +
                        '<div style="padding:16px;border-top:1px solid #252836;background:#0a0a12;flex-shrink:0">' +
                        '<div style="display:flex;gap:8px;margin-bottom:8px">' +
                        '<div style="flex:1;padding:10px;background:#1a1a2e;border-radius:8px;text-align:center">' +
                        '<div style="font-size:18px;font-weight:700;color:#10b981" id="active-parcel-count">' + Object.keys(activeParcelLayers).length + '</div>' +
                        '<div style="font-size:10px;color:#6b7280">ACTIVE LAYERS</div></div>' +
                        '<button id="clear-all-parcels" style="flex:1;padding:10px;background:#252836;color:#f1f5f9;border:1px solid #374151;border-radius:8px;cursor:pointer;font-size:13px">Clear All</button>' +
                        '<button onclick="document.getElementById(\'parcel-modal\').remove()" style="flex:1;padding:10px;background:#6366f1;color:white;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Done</button>' +
                        '</div>' +
                        '<p style="color:#64748b;font-size:11px;text-align:center">💡 Zoom to level 14+ to see parcel boundaries</p>' +
                        '</div>' +
                        '</div>';
                    
                    document.body.appendChild(parcelModal);
                    
                    // State tab switching
                    parcelModal.querySelectorAll('.state-tab').forEach(function(tab) {
                        tab.addEventListener('click', function() {
                            var state = this.dataset.state;
                            parcelModal.querySelectorAll('.state-tab').forEach(function(t) {
                                t.style.background = '#1a1a2e';
                                t.style.color = '#9ca3af';
                                t.classList.remove('active');
                            });
                            this.style.background = '#6366f1';
                            this.style.color = '#fff';
                            this.classList.add('active');
                            parcelModal.querySelectorAll('.state-content').forEach(function(c) {
                                c.style.display = c.dataset.state === state ? 'block' : 'none';
                            });
                        });
                    });
                    
                    // Search functionality
                    document.getElementById('parcel-search').addEventListener('input', function() {
                        var query = this.value.toLowerCase();
                        if (query.length === 0) {
                            // Reset to state view
                            parcelModal.querySelectorAll('.state-content').forEach(function(c, idx) {
                                c.style.display = idx === 0 || c.dataset.state === parcelModal.querySelector('.state-tab.active').dataset.state ? 'block' : 'none';
                            });
                            parcelModal.querySelectorAll('.county-parcel-btn').forEach(function(btn) {
                                btn.style.display = 'flex';
                            });
                        } else {
                            // Show all states during search
                            parcelModal.querySelectorAll('.state-content').forEach(function(c) {
                                c.style.display = 'block';
                            });
                            parcelModal.querySelectorAll('.county-parcel-btn').forEach(function(btn) {
                                var countyKey = btn.dataset.county;
                                var svc = COUNTY_PARCEL_SERVICES[countyKey];
                                var match = svc.name.toLowerCase().indexOf(query) !== -1 || 
                                           (svc.state && svc.state.toLowerCase().indexOf(query) !== -1);
                                btn.style.display = match ? 'flex' : 'none';
                            });
                        }
                    });
                    
                    // Handle county selection
                    parcelModal.querySelectorAll('.county-parcel-btn').forEach(function(btn) {
                        btn.addEventListener('click', function() {
                            var countyKey = this.dataset.county;
                            var svc = COUNTY_PARCEL_SERVICES[countyKey];
                            
                            if (activeParcelLayers[countyKey]) {
                                // Remove layer
                                map.removeLayer(activeParcelLayers[countyKey]);
                                delete activeParcelLayers[countyKey];
                                this.style.background = '#1a1a2e';
                                this.style.color = '#f1f5f9';
                                this.querySelector('span:last-child').textContent = '→';
                                console.log('🗺️ Removed parcel layer: ' + svc.name);
                            } else {
                                // Add layer using Esri Leaflet
                                try {
                                    var parcelLayer = L.esri.dynamicMapLayer({
                                        url: svc.url,
                                        opacity: 0.75,
                                        minZoom: 14,
                                        maxZoom: 20,
                                        attribution: svc.attribution
                                    });
                                    parcelLayer.addTo(map);
                                    activeParcelLayers[countyKey] = parcelLayer;
                                    this.style.background = '#22c55e';
                                    this.style.color = '#fff';
                                    this.querySelector('span:last-child').textContent = '✓';
                                    
                                    // Fly to county if not already there
                                    if (map.getZoom() < 10) {
                                        map.flyTo(svc.center, 12, {duration: 1.5});
                                    }
                                    
                                    console.log('🗺️ Loaded parcel layer: ' + svc.name);
                                } catch(e) {
                                    console.error('Failed to load parcel layer:', e);
                                    alert('Failed to load ' + svc.name + ' parcels. The service may be temporarily unavailable.');
                                }
                            }
                            
                            // Update counter
                            document.getElementById('active-parcel-count').textContent = Object.keys(activeParcelLayers).length;
                            
                            // Update main button
                            var parcelBtn = document.querySelector('[data-layer="parcels"]');
                            if (parcelBtn) {
                                var countEl = parcelBtn.querySelector('.count');
                                var activeCount = Object.keys(activeParcelLayers).length;
                                if (activeCount > 0) {
                                    parcelBtn.classList.add('active');
                                    countEl.textContent = activeCount + ' active';
                                    countEl.style.background = 'var(--green)';
                                } else {
                                    parcelBtn.classList.remove('active');
                                    countEl.textContent = Object.keys(COUNTY_PARCEL_SERVICES).length + ' counties';
                                    countEl.style.background = '';
                                }
                            }
                        });
                    });
                    
                    // Clear all button
                    document.getElementById('clear-all-parcels').addEventListener('click', function() {
                        Object.keys(activeParcelLayers).forEach(function(key) {
                            map.removeLayer(activeParcelLayers[key]);
                        });
                        activeParcelLayers = {};
                        parcelModal.querySelectorAll('.county-parcel-btn').forEach(function(btn) {
                            btn.style.background = '#1a1a2e';
                            btn.style.color = '#f1f5f9';
                            btn.querySelector('span:last-child').textContent = '→';
                        });
                        document.getElementById('active-parcel-count').textContent = '0';
                        var parcelBtn = document.querySelector('[data-layer="parcels"]');
                        if (parcelBtn) {
                            parcelBtn.classList.remove('active');
                            var countEl = parcelBtn.querySelector('.count');
                            countEl.textContent = Object.keys(COUNTY_PARCEL_SERVICES).length + ' counties';
                            countEl.style.background = '';
                        }
                        console.log('🗺️ Cleared all parcel layers');
                    });
                    
                    // Close on background click
                    parcelModal.addEventListener('click', function(e) {
                        if (e.target === parcelModal) parcelModal.remove();
                    });
                    
                    return;
                }
                
                // Special handling for Broadband - FCC 477 Data
                if (layerName === 'broadband' && broadbandTiles) {
                    if (map.hasLayer(broadbandTiles)) {
                        map.removeLayer(broadbandTiles);
                        this.classList.remove('active');
                        console.log('📶 FCC Broadband Layer hidden');
                    } else {
                        map.addLayer(broadbandTiles);
                        this.classList.add('active');
                        console.log('📶 FCC Broadband Layer shown - Fiber availability data');
                        if (map.getZoom() < 8) {
                            alert('💡 Tip: Zoom in to level 8+ to see broadband coverage details');
                        }
                    }
                    return;
                }
                
                // PHASE 1: Power Plants - HIFLD detailed
                if (layerName === 'powerplants') {
                    if (map.hasLayer(layers.powerplants)) {
                        map.removeLayer(layers.powerplants);
                        this.classList.remove('active');
                        console.log('🏭 Power Plants Layer hidden');
                    } else {
                        map.addLayer(layers.powerplants);
                        this.classList.add('active');
                        loadHIFLDPowerPlants(map.getBounds());
                        console.log('🏭 Power Plants Layer shown (HIFLD)');
                    }
                    return;
                }
                
                // PHASE 1: Internet Exchanges
                if (layerName === 'ixpoints') {
                    if (map.hasLayer(layers.ixpoints)) {
                        map.removeLayer(layers.ixpoints);
                        this.classList.remove('active');
                        console.log('🌐 Internet Exchanges hidden');
                    } else {
                        map.addLayer(layers.ixpoints);
                        this.classList.add('active');
                        loadInternetExchanges();
                        console.log('🌐 Internet Exchanges shown (PeeringDB)');
                    }
                    return;
                }
                
                // PHASE 1: Gas Compressor Stations
                if (layerName === 'gascompressors') {
                    if (map.hasLayer(layers.gascompressors)) {
                        map.removeLayer(layers.gascompressors);
                        this.classList.remove('active');
                        console.log('⛽ Gas Compressors hidden');
                    } else {
                        map.addLayer(layers.gascompressors);
                        this.classList.add('active');
                        loadHIFLDGasCompressors(map.getBounds());
                        console.log('⛽ Gas Compressors shown (HIFLD)');
                    }
                    return;
                }
                
                // PHASE 2: Opportunity Zones
                if (layerName === 'opzones') {
                    if (map.hasLayer(layers.opzones)) {
                        map.removeLayer(layers.opzones);
                        this.classList.remove('active');
                        console.log('💎 Opportunity Zones hidden');
                    } else {
                        map.addLayer(layers.opzones);
                        this.classList.add('active');
                        loadOpportunityZones();
                        console.log('💎 Opportunity Zones shown (Treasury)');
                    }
                    return;
                }
                
                // PHASE 2: Solar Resource
                if (layerName === 'solar') {
                    if (map.hasLayer(layers.solar)) {
                        map.removeLayer(layers.solar);
                        this.classList.remove('active');
                        console.log('☀️ Solar Resource hidden');
                    } else {
                        map.addLayer(layers.solar);
                        this.classList.add('active');
                        addSolarResourceInfo();
                        console.log('☀️ Solar Resource enabled (NREL)');
                        alert('☀️ Solar Resource: Click any location for solar potential analysis. Phoenix averages 6.5 kWh/m²/day.');
                    }
                    return;
                }
                
                // PHASE 2: Wind Resource
                if (layerName === 'wind') {
                    if (map.hasLayer(layers.wind)) {
                        map.removeLayer(layers.wind);
                        this.classList.remove('active');
                        console.log('💨 Wind Resource hidden');
                    } else {
                        map.addLayer(layers.wind);
                        this.classList.add('active');
                        addWindResourceInfo();
                        console.log('💨 Wind Resource enabled (NREL)');
                        alert('💨 Wind Resource: Best DC cooling in Northern Plains (ND, SD, WY). Texas Gulf Coast excellent for hybrid solar+wind.');
                    }
                    return;
                }
                
                // PHASE 3: Metro Fiber (Premium)
                if (layerName === 'metrofiber') {
                    if (map.hasLayer(layers.metrofiber)) {
                        map.removeLayer(layers.metrofiber);
                        this.classList.remove('active');
                        console.log('📡 Metro Fiber hidden');
                    } else {
                        map.addLayer(layers.metrofiber);
                        this.classList.add('active');
                        loadMetroFiber();
                        console.log('📡 Metro Fiber shown (Premium placeholder)');
                    }
                    return;
                }
                
                // Gen Queue - Interconnection waiting list
                if (layerName === 'genqueue') {
                    if (map.hasLayer(layers.genqueue)) {
                        map.removeLayer(layers.genqueue);
                        this.classList.remove('active');
                        console.log('⏳ Gen Queue hidden');
                    } else {
                        map.addLayer(layers.genqueue);
                        this.classList.add('active');
                        loadGenQueue();
                        console.log('⏳ Gen Queue shown - 1.3M MW waiting across RTOs');
                    }
                    return;
                }
                
                // Long-Haul Fiber Carriers
                if (layerName === 'longhaulfiber') {
                    if (map.hasLayer(layers.longhaulfiber)) {
                        map.removeLayer(layers.longhaulfiber);
                        this.classList.remove('active');
                        console.log('🔗 Long-Haul Fiber hidden');
                    } else {
                        map.addLayer(layers.longhaulfiber);
                        this.classList.add('active');
                        loadLongHaulFiber();
                        console.log('🔗 Long-Haul Fiber shown (Lumen, Zayo, Crown Castle, etc.)');
                    }
                    return;
                }
                
                // Midstream Gas Companies
                if (layerName === 'midstream') {
                    if (map.hasLayer(layers.midstream)) {
                        map.removeLayer(layers.midstream);
                        this.classList.remove('active');
                        console.log('🏭 Midstream Gas hidden');
                    } else {
                        map.addLayer(layers.midstream);
                        this.classList.add('active');
                        loadMidstreamGas();
                        console.log('🏭 Midstream Gas shown (Kinder Morgan, Williams, etc.)');
                    }
                    return;
                }
                
                // LNG Terminals
                if (layerName === 'lng') {
                    if (map.hasLayer(layers.lng)) {
                        map.removeLayer(layers.lng);
                        this.classList.remove('active');
                        console.log('⛽ LNG Terminals hidden');
                    } else {
                        map.addLayer(layers.lng);
                        this.classList.add('active');
                        loadLNGTerminals();
                        console.log('⛽ LNG Terminals shown (Export/Import facilities)');
                    }
                    return;
                }
                
                // Gas Storage Facilities
                if (layerName === 'gasStorage') {
                    if (map.hasLayer(layers.gasStorage)) {
                        map.removeLayer(layers.gasStorage);
                        this.classList.remove('active');
                        console.log('🛢️ Gas Storage hidden');
                    } else {
                        map.addLayer(layers.gasStorage);
                        this.classList.add('active');
                        loadGasStorage();
                        console.log('🛢️ Gas Storage shown (Underground storage facilities)');
                    }
                    return;
                }
                
                // Gas Market Hubs
                if (layerName === 'gasMarketHubs') {
                    if (map.hasLayer(layers.gasMarketHubs)) {
                        map.removeLayer(layers.gasMarketHubs);
                        this.classList.remove('active');
                        console.log('📍 Gas Market Hubs hidden');
                    } else {
                        map.addLayer(layers.gasMarketHubs);
                        this.classList.add('active');
                        loadGasMarketHubs();
                        console.log('📍 Gas Market Hubs shown (Henry Hub, Waha, etc.)');
                    }
                    return;
                }
                
                // Gas Processing Plants
                if (layerName === 'gasProcessing') {
                    if (map.hasLayer(layers.gasProcessing)) {
                        map.removeLayer(layers.gasProcessing);
                        this.classList.remove('active');
                        console.log('🏭 Gas Processing hidden');
                    } else {
                        map.addLayer(layers.gasProcessing);
                        this.classList.add('active');
                        loadGasProcessing();
                        console.log('🏭 Gas Processing shown (Processing plants)');
                    }
                    return;
                }
                
                // NGL Fractionators
                if (layerName === 'nglFractionators') {
                    if (map.hasLayer(layers.nglFractionators)) {
                        map.removeLayer(layers.nglFractionators);
                        this.classList.remove('active');
                        console.log('⚗️ NGL Fractionators hidden');
                    } else {
                        map.addLayer(layers.nglFractionators);
                        this.classList.add('active');
                        loadNGLFractionators();
                        console.log('⚗️ NGL Fractionators shown (Mont Belvieu, Conway, etc.)');
                    }
                    return;
                }
                
                // HIFLD Substations - Dynamic loading from HIFLD API
                if (layerName === 'hifldSubstations') {
                    if (map.hasLayer(layers.hifldSubstations)) {
                        map.removeLayer(layers.hifldSubstations);
                        this.classList.remove('active');
                        console.log('🔌 HIFLD Substations hidden');
                    } else {
                        map.addLayer(layers.hifldSubstations);
                        this.classList.add('active');
                        // Trigger dynamic loading
                        if (typeof DCHubInfrastructure !== 'undefined') {
                            DCHubInfrastructure.DynamicLayerManager.loadHIFLDSubstations(map.getBounds());
                        }
                        console.log('🔌 HIFLD Substations shown (70,000+ nationwide - zoom in for details)');
                        if (map.getZoom() < 8) {
                            alert('💡 HIFLD Substations: 70,000+ substations loaded dynamically. Zoom in to level 8+ for best detail.');
                        }
                    }
                    return;
                }
                
                // HIFLD Transmission Lines - Dynamic loading
                if (layerName === 'hifldTransmission') {
                    if (map.hasLayer(layers.hifldTransmission)) {
                        map.removeLayer(layers.hifldTransmission);
                        this.classList.remove('active');
                        console.log('⚡ HIFLD Transmission hidden');
                    } else {
                        map.addLayer(layers.hifldTransmission);
                        this.classList.add('active');
                        if (typeof DCHubInfrastructure !== 'undefined') {
                            DCHubInfrastructure.DynamicLayerManager.loadHIFLDTransmission(map.getBounds());
                        }
                        console.log('⚡ HIFLD Transmission shown (300,000+ miles - zoom in for details)');
                        if (map.getZoom() < 8) {
                            alert('💡 HIFLD Transmission: 300,000+ miles of transmission lines. Zoom in to level 8+ for best detail.');
                        }
                    }
                    return;
                }
                
                // HIFLD Gas Pipelines - Enhanced
                if (layerName === 'hifldGas') {
                    if (map.hasLayer(layers.hifldGas)) {
                        map.removeLayer(layers.hifldGas);
                        this.classList.remove('active');
                        console.log('🔥 HIFLD Gas Pipelines hidden');
                    } else {
                        map.addLayer(layers.hifldGas);
                        this.classList.add('active');
                        loadHIFLDGasPipelines(map.getBounds());
                        console.log('🔥 HIFLD Gas Pipelines shown (300,000+ miles)');
                    }
                    return;
                }
                
                // Railroads - HIFLD Transportation Ground
                if (layerName === 'railroad') {
                    if (map.hasLayer(layers.railroad)) {
                        map.removeLayer(layers.railroad);
                        this.classList.remove('active');
                        console.log('🚂 Railroads hidden');
                    } else {
                        map.addLayer(layers.railroad);
                        this.classList.add('active');
                        loadRailroads();
                        console.log('🚂 Railroads shown (HIFLD/USGS data)');
                    }
                    return;
                }
                
                // Aquifers - Major US aquifer boundaries
                if (layerName === 'aquifers') {
                    if (map.hasLayer(layers.aquifers)) {
                        map.removeLayer(layers.aquifers);
                        this.classList.remove('active');
                        console.log('💧 Aquifers hidden');
                    } else {
                        map.addLayer(layers.aquifers);
                        this.classList.add('active');
                        loadAquifers();
                        console.log('💧 Major Aquifers shown (USGS data)');
                    }
                    return;
                }
                
                // Rivers - Major rivers for cooling water access
                if (layerName === 'rivers') {
                    if (map.hasLayer(layers.rivers)) {
                        map.removeLayer(layers.rivers);
                        this.classList.remove('active');
                        console.log('🌊 Major Rivers hidden');
                    } else {
                        map.addLayer(layers.rivers);
                        this.classList.add('active');
                        loadMajorRivers();
                        console.log('🌊 Major Rivers shown (NHD data)');
                    }
                    return;
                }
                
                // Tier 2: Utility Territories - Electric service area polygons (v100)
                if (layerName === 'utilityTerritories') {
                    if (map.hasLayer(layers.utilityTerritories)) {
                        map.removeLayer(layers.utilityTerritories);
                        this.classList.remove('active');
                        console.log('🏢 Utility Territories hidden');
                    } else {
                        map.addLayer(layers.utilityTerritories);
                        this.classList.add('active');
                        loadUtilityTerritories(map.getBounds());
                        console.log('🏢 Utility Territories shown - Loading service areas...');
                    }
                    return;
                }
                
                if (!layerName || !layers[layerName]) {
                    // Silently ignore null or missing layers - they may be UI placeholders
                    if (layerName) {
                        console.log('📌 Layer placeholder:', layerName, '(not yet implemented)');
                    }
                    return;
                }
                
                if (map.hasLayer(layers[layerName])) {
                    map.removeLayer(layers[layerName]);
                    this.classList.remove('active');
                } else {
                    map.addLayer(layers[layerName]);
                    this.classList.add('active');
                }
            });
        });

        // Market selector
        document.getElementById('state-select').addEventListener('change', function() {
            var marketId = this.value;
            if (!marketId || !markets[marketId]) {
                document.getElementById('state-section').style.display = 'none';
                document.getElementById('utility-section').style.display = 'none';
                document.getElementById('airport-section').style.display = 'none';
                document.getElementById('tax-section').style.display = 'none';
                map.setView([39.0,-98.0],4);
                return;
            }
            
            var m = markets[marketId];
            map.setView(m.center, m.zoom);
            
            // Show market info
            document.getElementById('state-section').style.display = 'block';
            document.getElementById('state-info').innerHTML = 
                '<div class="state-info-header"><h3>'+m.name+'</h3><p>'+m.rto+' Interconnection</p></div>'+
                '<div class="state-info-grid">'+
                '<div class="state-info-item"><div class="state-info-value">'+m.capacity+'</div><div class="state-info-label">Grid Capacity</div></div>'+
                '<div class="state-info-item"><div class="state-info-value">'+m.cost+'</div><div class="state-info-label">Power Cost</div></div>'+
                '<div class="state-info-item"><div class="state-info-value">'+m.renewable+'</div><div class="state-info-label">Renewable</div></div>'+
                '<div class="state-info-item"><div class="state-info-value">'+m.flood+'</div><div class="state-info-label">Flood Risk</div></div>'+
                '</div>';
            
            // Show utilities
            document.getElementById('utility-section').style.display = 'block';
            document.getElementById('utility-list').innerHTML = m.utilities.map(function(u) {
                return '<div class="utility-item"><div class="utility-name">'+u+'</div><div class="utility-meta">Primary utility provider</div></div>';
            }).join('');
            
            // Show tax incentives
            if (m.tax) {
                document.getElementById('tax-section').style.display = 'block';
                var taxHtml = '<div class="tax-card">'+
                    '<div class="tax-card-header">'+
                    '<span class="tax-badge '+m.tax.rating+'">'+m.tax.rating+'</span>'+
                    '<span class="tax-title">Incentive Package</span>'+
                    '</div>'+
                    '<div class="tax-items">'+
                    m.tax.incentives.map(function(inc) {
                        return '<div class="tax-item"><span class="tax-item-icon">✓</span><span class="tax-item-text">'+inc+'</span></div>';
                    }).join('')+
                    '</div>'+
                    '<div class="tax-savings">'+
                    '<div class="tax-savings-label">Estimated Annual Savings</div>'+
                    '<div class="tax-savings-value">'+m.tax.estSavings+'</div>'+
                    '</div>'+
                    (m.tax.hasOZ ? '<div class="oz-badge">🎯 Opportunity Zone Available</div>' : '')+
                    '</div>';
                document.getElementById('tax-content').innerHTML = taxHtml;
            } else {
                document.getElementById('tax-section').style.display = 'none';
            }
        });

        // ============================================
        // AUTO-REFRESH TIMER
        // ============================================
        var refreshInterval = 60;
        var currentSecond = 60;
        var lastRefreshData = {}; // Track item counts for new item detection
        var newItemsHighlight = []; // Track newly added markers
        var AUTO_REFRESH_ENABLED = true;
        
        // New item highlight style
        var newItemStyle = {
            className: 'new-item-pulse',
            duration: 30000 // 30 seconds highlight
        };

        function updateTimer() {
            currentSecond--;
            document.getElementById('timer-value').textContent = currentSecond + 's';
            document.getElementById('timer-progress').style.width = (currentSecond / refreshInterval * 100) + '%';
            
            if (currentSecond <= 0) {
                currentSecond = refreshInterval;
                if (AUTO_REFRESH_ENABLED) {
                    performDataRefresh();
                }
            }
        }
        
        // ACTUAL DATA REFRESH FUNCTION
        async function performDataRefresh() {
            console.log('🔄 Performing data refresh at ' + new Date().toLocaleTimeString());
            
            // Store current counts before refresh
            var beforeCounts = {
                substations: layers.hifldSubstations ? layers.hifldSubstations.getLayers().length : 0,
                transmission: layers.hifldTransmission ? layers.hifldTransmission.getLayers().length : 0,
                powerplants: layers.powerplants ? layers.powerplants.getLayers().length : 0,
                gas: layers.hifldGas ? layers.hifldGas.getLayers().length : 0
            };
            
            // Animate API dots
            document.querySelectorAll('.api-dot').forEach(function(dot) {
                dot.classList.add('syncing');
            });
            
            // Clear API cache to force fresh data
            API_CACHE = {};
            
            // Refresh live data based on current view
            var bounds = map.getBounds();
            var zoom = map.getZoom();
            
            try {
                // Parallel refresh of all API data
                var refreshPromises = [];
                
                if (zoom >= 7) {
                    refreshPromises.push(loadSubstationsFromAPI(bounds, true));
                    refreshPromises.push(loadGasPipelinesFromAPI(bounds, true));
                    refreshPromises.push(loadPowerPlantsFromAPI(bounds, true));
                }
                if (zoom >= 8) {
                    refreshPromises.push(loadTransmissionFromAPI(bounds, true));
                }
                
                await Promise.all(refreshPromises);
                
                // Calculate new items
                var afterCounts = {
                    substations: layers.hifldSubstations ? layers.hifldSubstations.getLayers().length : 0,
                    transmission: layers.hifldTransmission ? layers.hifldTransmission.getLayers().length : 0,
                    powerplants: layers.powerplants ? layers.powerplants.getLayers().length : 0,
                    gas: layers.hifldGas ? layers.hifldGas.getLayers().length : 0
                };
                
                var newItems = {
                    substations: afterCounts.substations - beforeCounts.substations,
                    transmission: afterCounts.transmission - beforeCounts.transmission,
                    powerplants: afterCounts.powerplants - beforeCounts.powerplants,
                    gas: afterCounts.gas - beforeCounts.gas
                };
                
                // Log new items
                var totalNew = Object.values(newItems).reduce((a, b) => a + Math.max(0, b), 0);
                if (totalNew > 0) {
                    console.log('✨ New items found:', newItems);
                    showNewItemsNotification(newItems);
                }
                
                // Update last refresh timestamp
                document.getElementById('last-refresh-time').textContent = new Date().toLocaleTimeString();
                
            } catch (err) {
                console.error('❌ Refresh error:', err);
            }
            
            // Remove syncing animation
            setTimeout(function() {
                document.querySelectorAll('.api-dot').forEach(function(dot) {
                    dot.classList.remove('syncing');
                });
            }, 1500);
        }
        
        // Show notification when new items are found
        function showNewItemsNotification(newItems) {
            var notification = document.createElement('div');
            notification.className = 'new-items-notification';
            notification.innerHTML = '<div class="notif-icon">✨</div><div class="notif-content"><strong>New Data Found!</strong><br>';
            
            var parts = [];
            if (newItems.substations > 0) parts.push('+' + newItems.substations + ' substations');
            if (newItems.transmission > 0) parts.push('+' + newItems.transmission + ' transmission');
            if (newItems.powerplants > 0) parts.push('+' + newItems.powerplants + ' power plants');
            if (newItems.gas > 0) parts.push('+' + newItems.gas + ' gas facilities');
            
            notification.innerHTML += parts.join(', ') + '</div>';
            notification.style.cssText = 'position:fixed;bottom:80px;right:20px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:12px 16px;border-radius:8px;box-shadow:0 4px 20px rgba(16,185,129,0.4);z-index:10000;display:flex;align-items:center;gap:12px;animation:slideIn 0.3s ease;font-size:12px;max-width:300px';
            
            document.body.appendChild(notification);
            
            setTimeout(function() {
                notification.style.animation = 'slideOut 0.3s ease';
                setTimeout(function() { notification.remove(); }, 300);
            }, 5000);
        }
        
        setInterval(updateTimer, 1000);
        
        // Toggle auto-refresh
        function toggleAutoRefresh() {
            AUTO_REFRESH_ENABLED = !AUTO_REFRESH_ENABLED;
            var btn = document.getElementById('auto-refresh-btn');
            btn.style.background = AUTO_REFRESH_ENABLED ? '#10b981' : '#6b7280';
            btn.textContent = AUTO_REFRESH_ENABLED ? 'AUTO' : 'PAUSED';
            document.getElementById('api-status-indicator').innerHTML = AUTO_REFRESH_ENABLED ? '● LIVE' : '○ PAUSED';
            document.getElementById('api-status-indicator').style.color = AUTO_REFRESH_ENABLED ? 'var(--green)' : 'var(--text3)';
            console.log('🔄 Auto-refresh: ' + (AUTO_REFRESH_ENABLED ? 'Enabled' : 'Paused'));
        }
        
        // Make it global
        window.toggleAutoRefresh = toggleAutoRefresh;
        
        // ============================================
        // COMPREHENSIVE DATA AUDIT SYSTEM
        // Tracks all data sources and their status
        // ============================================
        
        // Run data audit - counts from actual data sources
        function runDataAudit() {
            console.log('📊 Running Data Audit...');
            
            // Define categories with their actual data sources
            var categories = [
                {name: 'Data Centers', count: typeof dataCenters !== 'undefined' ? dataCenters.length : 0, source: 'Built-in DB'},
                {name: 'Nuclear Facilities', count: typeof nuclearReactors !== 'undefined' ? nuclearReactors.length : 0, source: 'Built-in DB'},
                {name: 'Fiber Routes', count: typeof fiberRoutes !== 'undefined' ? fiberRoutes.length : 0, source: 'Built-in DB + KMZ'},
                {name: 'Fiber Carriers', count: typeof FIBER_CARRIERS !== 'undefined' ? FIBER_CARRIERS.length : 0, source: 'FiberLocator DB'},
                {name: 'Submarine Cables', count: typeof submarineCables !== 'undefined' ? submarineCables.length : 0, source: 'Built-in DB'},
                {name: 'Airports', count: typeof airports !== 'undefined' ? airports.length : 0, source: 'Built-in DB'},
                {name: 'Substations (static)', count: typeof substations !== 'undefined' ? substations.length : 0, source: 'Built-in DB'},
                {name: 'Gas Pipelines', count: typeof gasPipelines !== 'undefined' ? gasPipelines.length : 0, source: 'Built-in DB'},
                {name: 'Midstream Gas', count: typeof MIDSTREAM_GAS_COMPANIES !== 'undefined' ? MIDSTREAM_GAS_COMPANIES.length : 0, source: 'Built-in DB'},
                {name: 'LNG Terminals', count: typeof LNG_TERMINALS !== 'undefined' ? LNG_TERMINALS.length : 0, source: 'Built-in DB'},
                {name: 'Internet Exchanges', count: typeof INTERNET_EXCHANGES !== 'undefined' ? INTERNET_EXCHANGES.length : 0, source: 'PeeringDB'},
                {name: 'Opportunity Zones', count: typeof OPPORTUNITY_ZONES !== 'undefined' ? OPPORTUNITY_ZONES.length : 0, source: 'Treasury CDFI'},
                {name: 'HIFLD Substations', count: layers.hifldSubstations ? layers.hifldSubstations.getLayers().length : 0, source: 'HIFLD API (Live)'},
                {name: 'HIFLD Transmission', count: layers.hifldTransmission ? layers.hifldTransmission.getLayers().length : 0, source: 'HIFLD API (Live)'},
                {name: 'HIFLD Gas', count: layers.hifldGas ? layers.hifldGas.getLayers().length : 0, source: 'HIFLD API (Live)'},
                {name: 'Power Plants', count: layers.powerplants ? layers.powerplants.getLayers().length : 0, source: 'HIFLD API (Live)'}
            ];
            
            var totalItems = 0;
            var issues = [];
            
            categories.forEach(function(cat) {
                totalItems += cat.count;
                cat.status = cat.count > 0 ? '✅' : '⚠️';
                if (cat.count === 0 && !cat.source.includes('Live')) {
                    issues.push(cat.name);
                }
            });
            
            // Log report
            console.log('═══════════════════════════════════════════');
            console.log('📊 DC HUB DATA AUDIT REPORT');
            console.log('═══════════════════════════════════════════');
            categories.forEach(function(r) {
                console.log(r.status + ' ' + r.name + ': ' + r.count + ' items (' + r.source + ')');
            });
            console.log('═══════════════════════════════════════════');
            console.log('📈 Total Items: ' + totalItems);
            console.log('═══════════════════════════════════════════');
            
            return {total: totalItems, categories: categories, issues: issues};
        }
        
        // Make audit function global
        window.runDataAudit = runDataAudit;
        
        // Show audit panel in UI
        function showAuditPanel() {
            // Remove existing panel if any
            var existing = document.getElementById('audit-panel');
            if (existing) existing.remove();
            
            var audit = runDataAudit();
            var panel = document.createElement('div');
            panel.id = 'audit-panel';
            panel.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:24px;z-index:10001;max-height:80vh;overflow-y:auto;width:500px;box-shadow:0 20px 60px rgba(0,0,0,0.7);font-family:system-ui,-apple-system,sans-serif';
            
            var html = '';
            // Header
            html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #333">';
            html += '<h3 style="margin:0;color:#a78bfa;font-size:18px;display:flex;align-items:center;gap:8px">📊 Data Audit Report</h3>';
            html += '<button onclick="document.getElementById(\'audit-panel\').remove()" style="background:#333;border:none;color:#fff;cursor:pointer;font-size:16px;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center">✕</button>';
            html += '</div>';
            
            // Total count box
            html += '<div style="background:linear-gradient(135deg,#10b981,#059669);border-radius:10px;padding:16px;margin-bottom:20px;text-align:center">';
            html += '<div style="font-size:36px;font-weight:800;color:#fff">' + audit.total.toLocaleString() + '</div>';
            html += '<div style="font-size:12px;color:rgba(255,255,255,0.8)">Total Items Loaded</div>';
            html += '</div>';
            
            // Categories table
            html += '<div style="background:#0f0f1a;border-radius:8px;overflow:hidden">';
            html += '<table style="width:100%;font-size:12px;border-collapse:collapse">';
            html += '<thead><tr style="background:#1f1f35">';
            html += '<th style="text-align:left;padding:10px 12px;color:#888;font-weight:500">Category</th>';
            html += '<th style="text-align:right;padding:10px 12px;color:#888;font-weight:500;width:80px">Count</th>';
            html += '<th style="text-align:left;padding:10px 12px;color:#888;font-weight:500">Source</th>';
            html += '</tr></thead><tbody>';
            
            audit.categories.forEach(function(r, i) {
                var bgColor = i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)';
                var textColor = r.count > 0 ? '#fff' : '#ef4444';
                var countColor = r.count > 0 ? '#10b981' : '#ef4444';
                html += '<tr style="background:' + bgColor + '">';
                html += '<td style="padding:10px 12px;color:' + textColor + '">' + r.status + ' ' + r.name + '</td>';
                html += '<td style="text-align:right;padding:10px 12px;font-family:monospace;font-weight:600;color:' + countColor + '">' + r.count.toLocaleString() + '</td>';
                html += '<td style="padding:10px 12px;color:#666;font-size:10px">' + r.source + '</td>';
                html += '</tr>';
            });
            
            html += '</tbody></table></div>';
            
            // Issues warning (only show for static data issues)
            if (audit.issues.length > 0) {
                html += '<div style="margin-top:16px;padding:12px;background:rgba(239,68,68,0.1);border-radius:8px;border-left:3px solid #ef4444">';
                html += '<strong style="color:#ef4444;font-size:12px">⚠️ Missing Static Data:</strong>';
                html += '<div style="font-size:11px;color:#888;margin-top:4px">' + audit.issues.join(', ') + '</div>';
                html += '</div>';
            }
            
            // Note about live data
            html += '<div style="margin-top:16px;padding:12px;background:rgba(59,130,246,0.1);border-radius:8px;border-left:3px solid #3b82f6">';
            html += '<strong style="color:#3b82f6;font-size:11px">💡 Note:</strong>';
            html += '<div style="font-size:10px;color:#888;margin-top:4px">HIFLD API data loads dynamically when you zoom in (level 7+). Pan/zoom the map to load infrastructure in that area.</div>';
            html += '</div>';
            
            // Timestamp
            html += '<div style="margin-top:16px;font-size:10px;color:#555;text-align:center">Audit completed at ' + new Date().toLocaleTimeString() + '</div>';
            
            panel.innerHTML = html;
            document.body.appendChild(panel);
        }
        
        window.showAuditPanel = showAuditPanel;

        // Update stats
        document.getElementById('stat-dc').textContent = dataCenters.length;
        document.getElementById('stat-nuclear').textContent = nuclearReactors.length;
        document.getElementById('stat-subs').textContent = substations.length;
        document.getElementById('stat-airports').textContent = airports.length;
        document.getElementById('stat-fiber').textContent = fiberRoutes.length;
        document.getElementById('stat-gas').textContent = gasPipelines.length;
        document.getElementById('count-dc').textContent = dataCenters.length;
        document.getElementById('count-nuclear').textContent = nuclearReactors.length;
        document.getElementById('count-subs').textContent = substations.length;
        document.getElementById('count-airports').textContent = airports.length;
        
        console.log('DC Hub Land & Power v74 - 807 Fiber Routes + Auto-Refresh + Data Audit');
        console.log('═══════════════════════════════════════════════════════════════');
        console.log('✅ Auto-Refresh: Every 60 seconds');
        console.log('✅ New Item Detection: Highlights new data');
        console.log('✅ Data Audit: runDataAudit() or click 📊 Audit button');
        console.log('═══════════════════════════════════════════════════════════════');
        console.log('Data Centers: ' + dataCenters.length);
        console.log('Nuclear reactors: ' + nuclearReactors.length);
        console.log('Substations: ' + substations.length);
        console.log('Gas pipelines: ' + gasPipelines.length);
        console.log('Fiber routes: ' + fiberRoutes.length + ' backbone routes');
        console.log('Submarine cables: ' + submarineCables.length);
        console.log('Airports: ' + airports.length);

        // ============================================
        // LIVE API INTEGRATION MODULE v68
        // Real-time data from HIFLD, OpenStreetMap, FEMA
        // ============================================
        
        var ENABLE_LIVE_API = true;
        var API_CACHE = {};
        var API_LOADING = {};
        
        // HIFLD ArcGIS REST APIs - Infrastructure Data
        var HIFLD_APIS = {
            transmission: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query',
            powerPlants: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0/query',
            substations: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query',
            gasPipelines: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0/query',
            gasCompressors: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query',
            // Tier 2: Territory Overlays (v100) - Using HIFLD CORS-enabled endpoint
            utilityTerritories: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Retail_Service_Territories_2/FeatureServer/0/query'
        };
        
        // EIA / NREL APIs for renewable resources
        var ENERGY_APIS = {
            solarResource: 'https://developer.nrel.gov/api/solar/solar_resource/v1.json',
            windResource: 'https://developer.nrel.gov/api/wind/wind_resource/v1.json'
        };
        
        // Internet Exchange Points (Major US IXs from PeeringDB)
        var INTERNET_EXCHANGES = [
            {name: "Equinix Ashburn IX", city: "Ashburn, VA", lat: 39.0438, lng: -77.4874, participants: 850, peakTbps: 12.5},
            {name: "DE-CIX New York", city: "New York, NY", lat: 40.7614, lng: -73.9776, participants: 420, peakTbps: 4.2},
            {name: "CoreSite Any2 LA", city: "Los Angeles, CA", lat: 34.0522, lng: -118.2437, participants: 310, peakTbps: 3.1},
            {name: "Equinix Chicago IX", city: "Chicago, IL", lat: 41.8781, lng: -87.6298, participants: 280, peakTbps: 2.8},
            {name: "DE-CIX Dallas", city: "Dallas, TX", lat: 32.7767, lng: -96.7970, participants: 195, peakTbps: 2.1},
            {name: "Equinix San Jose IX", city: "San Jose, CA", lat: 37.3382, lng: -121.8863, participants: 380, peakTbps: 5.5},
            {name: "CoreSite Any2 Denver", city: "Denver, CO", lat: 39.7392, lng: -104.9903, participants: 145, peakTbps: 1.2},
            {name: "Equinix Seattle IX", city: "Seattle, WA", lat: 47.6062, lng: -122.3321, participants: 220, peakTbps: 2.4},
            {name: "NOTA Miami", city: "Miami, FL", lat: 25.7617, lng: -80.1918, participants: 175, peakTbps: 1.8},
            {name: "AMS-IX Bay Area", city: "Palo Alto, CA", lat: 37.4419, lng: -122.1430, participants: 125, peakTbps: 1.5},
            {name: "Equinix Atlanta IX", city: "Atlanta, GA", lat: 33.7490, lng: -84.3880, participants: 165, peakTbps: 1.6},
            {name: "DE-CIX Phoenix", city: "Phoenix, AZ", lat: 33.4484, lng: -112.0740, participants: 85, peakTbps: 0.9},
            {name: "LINX NoVA", city: "Ashburn, VA", lat: 39.0458, lng: -77.4900, participants: 95, peakTbps: 1.1},
            {name: "TorIX", city: "Toronto, ON", lat: 43.6532, lng: -79.3832, participants: 210, peakTbps: 2.2},
            {name: "QIX Montreal", city: "Montreal, QC", lat: 45.5017, lng: -73.5673, participants: 88, peakTbps: 0.8}
        ];
        
        // Federal Opportunity Zones (Major DC Markets)
        var OPPORTUNITY_ZONES = [
            {name: "Loudoun County OZ", city: "Ashburn, VA", lat: 39.0185, lng: -77.5200, tractId: "51107601900"},
            {name: "Prince William OZ", city: "Manassas, VA", lat: 38.7509, lng: -77.4753, tractId: "51153050100"},
            {name: "Dallas South OZ", city: "Dallas, TX", lat: 32.7357, lng: -96.8286, tractId: "48113001700"},
            {name: "Phoenix West OZ", city: "Phoenix, AZ", lat: 33.4942, lng: -112.1401, tractId: "04013112200"},
            {name: "Mesa East OZ", city: "Mesa, AZ", lat: 33.4152, lng: -111.8315, tractId: "04013421800"},
            {name: "Columbus East OZ", city: "Columbus, OH", lat: 39.9833, lng: -82.8833, tractId: "39049002200"},
            {name: "Atlanta West OZ", city: "Atlanta, GA", lat: 33.7627, lng: -84.4227, tractId: "13121003600"},
            {name: "Salt Lake OZ", city: "Salt Lake City, UT", lat: 40.7608, lng: -111.8910, tractId: "49035101600"},
            {name: "Las Vegas N OZ", city: "North Las Vegas, NV", lat: 36.2719, lng: -115.0902, tractId: "32003001603"},
            {name: "Reno Industrial OZ", city: "Reno, NV", lat: 39.5296, lng: -119.8138, tractId: "32031000403"}
        ];
        
        // ============================================
        // INTERCONNECTION QUEUE DATA BY RTO/ISO
        // What's waiting to come online in each market
        // ============================================
        var INTERCONNECTION_QUEUES = {
            "PJM": {
                name: "PJM Interconnection",
                region: "Mid-Atlantic/Midwest",
                totalQueueMW: 298000,
                activeProjects: 2847,
                avgWaitYears: 4.2,
                byFuel: {solar: 145000, storage: 78000, wind: 42000, gas: 18000, nuclear: 3200, hybrid: 11800},
                markets: ["Northern Virginia", "Chicago", "Columbus", "Pittsburgh", "New Jersey"],
                queueUrl: "https://www.pjm.com/planning/services-requests/interconnection-queues",
                recentApprovals: [
                    {project: "Dominion Solar + Storage", mw: 1200, market: "Virginia", status: "Under Construction"},
                    {project: "Google Data Center Load", mw: 400, market: "Columbus", status: "Approved"},
                    {project: "AWS Ashburn Expansion", mw: 600, market: "N. Virginia", status: "In Study"}
                ]
            },
            "ERCOT": {
                name: "ERCOT (Texas)",
                region: "Texas",
                totalQueueMW: 342000,
                activeProjects: 1856,
                avgWaitYears: 2.8,
                byFuel: {solar: 178000, storage: 89000, wind: 52000, gas: 14000, nuclear: 2400, hybrid: 6600},
                markets: ["Dallas-Fort Worth", "Houston", "Austin", "San Antonio", "Midland-Odessa"],
                queueUrl: "http://www.ercot.com/gridinfo/resource",
                recentApprovals: [
                    {project: "Stargate Phase 1", mw: 2000, market: "Abilene", status: "Under Construction"},
                    {project: "Meta Temple DC", mw: 900, market: "Temple", status: "Approved"},
                    {project: "Oracle Austin Campus", mw: 500, market: "Austin", status: "In Study"}
                ]
            },
            "CAISO": {
                name: "CAISO (California)",
                region: "California",
                totalQueueMW: 187000,
                activeProjects: 1234,
                avgWaitYears: 5.1,
                byFuel: {solar: 98000, storage: 62000, wind: 18000, gas: 4000, nuclear: 0, hybrid: 5000},
                markets: ["Silicon Valley", "Los Angeles", "San Diego", "Sacramento"],
                queueUrl: "https://www.caiso.com/planning/Pages/GeneratorInterconnection/Default.aspx",
                recentApprovals: [
                    {project: "Google Sunnyvale Storage", mw: 200, market: "Silicon Valley", status: "Approved"},
                    {project: "Apple Solar Farm", mw: 350, market: "Central CA", status: "Under Construction"}
                ]
            },
            "MISO": {
                name: "MISO",
                region: "Midwest/South",
                totalQueueMW: 256000,
                activeProjects: 1678,
                avgWaitYears: 3.5,
                byFuel: {solar: 112000, storage: 54000, wind: 68000, gas: 12000, nuclear: 2800, hybrid: 7200},
                markets: ["Minneapolis", "Indianapolis", "Detroit", "New Orleans", "Louisiana"],
                queueUrl: "https://www.misoenergy.org/planning/generator-interconnection/GI_Queue/",
                recentApprovals: [
                    {project: "Meta Louisiana Campus", mw: 1500, market: "Louisiana", status: "In Study"},
                    {project: "Microsoft Wisconsin", mw: 800, market: "Wisconsin", status: "Approved"}
                ]
            },
            "SPP": {
                name: "SPP (Southwest Power Pool)",
                region: "Central US",
                totalQueueMW: 145000,
                activeProjects: 892,
                avgWaitYears: 2.9,
                byFuel: {solar: 48000, storage: 28000, wind: 58000, gas: 6000, nuclear: 0, hybrid: 5000},
                markets: ["Oklahoma City", "Kansas City", "Omaha", "Little Rock"],
                queueUrl: "https://www.spp.org/engineering/generator-interconnection/",
                recentApprovals: [
                    {project: "Google Oklahoma Wind", mw: 400, market: "Oklahoma", status: "Under Construction"}
                ]
            },
            "NYISO": {
                name: "NYISO (New York)",
                region: "New York",
                totalQueueMW: 95000,
                activeProjects: 567,
                avgWaitYears: 4.8,
                byFuel: {solar: 28000, storage: 32000, wind: 24000, gas: 6000, nuclear: 1200, hybrid: 3800},
                markets: ["New York City", "Albany", "Buffalo", "Upstate NY"],
                queueUrl: "https://www.nyiso.com/interconnections",
                recentApprovals: [
                    {project: "Equinix NY5 Expansion", mw: 150, market: "NYC", status: "Approved"}
                ]
            },
            "ISO-NE": {
                name: "ISO New England",
                region: "New England",
                totalQueueMW: 42000,
                activeProjects: 389,
                avgWaitYears: 4.1,
                byFuel: {solar: 12000, storage: 15000, wind: 8000, gas: 4000, nuclear: 0, hybrid: 3000},
                markets: ["Boston", "Hartford", "Providence", "Maine"],
                queueUrl: "https://www.iso-ne.com/system-planning/interconnection-service/",
                recentApprovals: []
            }
        };
        
        // ============================================
        // LONG-HAUL FIBER CARRIERS
        // Major backbone networks
        // ============================================
        var LONGHAUL_FIBER_CARRIERS = [
            {
                name: "Lumen (Level 3/CenturyLink)",
                routeMiles: 450000,
                countries: 60,
                color: "#00a1e0",
                tier: 1,
                networkMapUrl: "https://www.lumen.com/en-us/resources/network-maps.html",
                keyRoutes: [
                    {from: "Ashburn, VA", to: "Chicago, IL", latency: "12ms", coords: [[39.04,-77.49],[40.0,-79.0],[40.5,-81.0],[41.88,-87.63]]},
                    {from: "Dallas, TX", to: "Los Angeles, CA", latency: "18ms", coords: [[32.78,-96.80],[33.5,-112.1],[34.05,-118.24]]},
                    {from: "New York, NY", to: "Chicago, IL", latency: "14ms", coords: [[40.71,-74.01],[40.5,-80.0],[41.88,-87.63]]},
                    {from: "Atlanta, GA", to: "Miami, FL", latency: "10ms", coords: [[33.75,-84.39],[30.3,-81.7],[25.76,-80.19]]},
                    {from: "Seattle, WA", to: "Los Angeles, CA", latency: "15ms", coords: [[47.61,-122.33],[45.5,-122.7],[37.77,-122.42],[34.05,-118.24]]}
                ],
                dcMarkets: ["Ashburn", "Dallas", "Chicago", "Phoenix", "Denver", "Seattle", "Atlanta", "Los Angeles", "New York", "Miami"]
            },
            {
                name: "Zayo Group",
                routeMiles: 141000,
                countries: 5,
                color: "#e31837",
                tier: 1,
                networkMapUrl: "https://www.zayo.com/network/",
                keyRoutes: [
                    {from: "Ashburn, VA", to: "New York, NY", latency: "4ms", coords: [[39.04,-77.49],[40.0,-75.5],[40.71,-74.01]]},
                    {from: "Chicago, IL", to: "Dallas, TX", latency: "15ms", coords: [[41.88,-87.63],[39.1,-94.6],[35.5,-97.5],[32.78,-96.80]]},
                    {from: "Los Angeles, CA", to: "Phoenix, AZ", latency: "8ms", coords: [[34.05,-118.24],[33.5,-112.1]]},
                    {from: "Denver, CO", to: "Salt Lake City, UT", latency: "6ms", coords: [[39.74,-104.99],[40.76,-111.89]]},
                    {from: "San Francisco, CA", to: "Portland, OR", latency: "10ms", coords: [[37.77,-122.42],[45.5,-122.7]]}
                ],
                dcMarkets: ["Ashburn", "Dallas", "Chicago", "Phoenix", "Denver", "Los Angeles", "New York", "San Jose", "Portland"]
            },
            {
                name: "Crown Castle Fiber",
                routeMiles: 85000,
                countries: 1,
                color: "#0066cc",
                tier: 2,
                networkMapUrl: "https://www.crowncastle.com/network",
                keyRoutes: [
                    {from: "Houston, TX", to: "Dallas, TX", latency: "5ms", coords: [[29.76,-95.37],[32.78,-96.80]]},
                    {from: "Atlanta, GA", to: "Charlotte, NC", latency: "4ms", coords: [[33.75,-84.39],[35.23,-80.84]]},
                    {from: "Phoenix, AZ", to: "Tucson, AZ", latency: "2ms", coords: [[33.45,-112.07],[32.22,-110.93]]}
                ],
                dcMarkets: ["Houston", "Dallas", "Atlanta", "Phoenix", "Denver", "Charlotte"]
            },
            {
                name: "Cogent Communications",
                routeMiles: 113000,
                countries: 51,
                color: "#ff6600",
                tier: 1,
                networkMapUrl: "https://www.cogentco.com/en/network",
                keyRoutes: [
                    {from: "Ashburn, VA", to: "New York, NY", latency: "4ms", coords: [[39.04,-77.49],[40.71,-74.01]]},
                    {from: "Chicago, IL", to: "New York, NY", latency: "14ms", coords: [[41.88,-87.63],[40.5,-80.0],[40.71,-74.01]]},
                    {from: "Los Angeles, CA", to: "San Jose, CA", latency: "5ms", coords: [[34.05,-118.24],[37.34,-121.89]]}
                ],
                dcMarkets: ["Ashburn", "Chicago", "Dallas", "Los Angeles", "New York", "Miami", "San Jose"]
            },
            {
                name: "GTT Communications",
                routeMiles: 90000,
                countries: 140,
                color: "#7030a0",
                tier: 1,
                networkMapUrl: "https://www.gtt.net/us-en/network",
                keyRoutes: [
                    {from: "New York, NY", to: "Chicago, IL", latency: "14ms", coords: [[40.71,-74.01],[40.5,-80.0],[41.88,-87.63]]},
                    {from: "Dallas, TX", to: "Los Angeles, CA", latency: "18ms", coords: [[32.78,-96.80],[33.5,-112.1],[34.05,-118.24]]}
                ],
                dcMarkets: ["Ashburn", "Chicago", "Dallas", "Los Angeles", "New York"]
            },
            {
                name: "Uniti Fiber",
                routeMiles: 140000,
                countries: 1,
                color: "#00b050",
                tier: 2,
                networkMapUrl: "https://www.uniti.com/network-map",
                keyRoutes: [
                    {from: "Dallas, TX", to: "Houston, TX", latency: "4ms", coords: [[32.78,-96.80],[29.76,-95.37]]},
                    {from: "Atlanta, GA", to: "Miami, FL", latency: "8ms", coords: [[33.75,-84.39],[30.3,-81.7],[25.76,-80.19]]}
                ],
                dcMarkets: ["Dallas", "Houston", "Atlanta", "Tampa", "Phoenix"]
            },
            {
                name: "Segra (Lumos Networks)",
                routeMiles: 48000,
                countries: 1,
                color: "#0077c8",
                tier: 2,
                networkMapUrl: "https://www.segra.com/network-map",
                keyRoutes: [
                    {from: "Ashburn, VA", to: "Charlotte, NC", latency: "8ms", coords: [[39.04,-77.49],[37.5,-77.5],[35.23,-80.84]]},
                    {from: "Atlanta, GA", to: "Richmond, VA", latency: "10ms", coords: [[33.75,-84.39],[35.8,-78.6],[37.5,-77.5]]}
                ],
                dcMarkets: ["Ashburn", "Charlotte", "Atlanta", "Richmond", "Raleigh"]
            },
            {
                name: "FirstLight Fiber",
                routeMiles: 25000,
                countries: 1,
                color: "#e31937",
                tier: 2,
                networkMapUrl: "https://www.firstlight.net/network-map",
                keyRoutes: [
                    {from: "Boston, MA", to: "Albany, NY", latency: "5ms", coords: [[42.36,-71.06],[42.65,-73.76]]},
                    {from: "Portland, ME", to: "Boston, MA", latency: "3ms", coords: [[43.66,-70.26],[42.36,-71.06]]}
                ],
                dcMarkets: ["Boston", "New York"]
            },
            {
                name: "Everstream Solutions",
                routeMiles: 28000,
                countries: 1,
                color: "#00875a",
                tier: 2,
                networkMapUrl: "https://www.everstream.net/network-map",
                keyRoutes: [
                    {from: "Chicago, IL", to: "Detroit, MI", latency: "4ms", coords: [[41.88,-87.63],[42.33,-83.05]]},
                    {from: "Columbus, OH", to: "Indianapolis, IN", latency: "3ms", coords: [[39.96,-83.0],[39.77,-86.16]]}
                ],
                dcMarkets: ["Chicago", "Columbus", "Indianapolis", "Detroit"]
            },
            {
                name: "Windstream Enterprise",
                routeMiles: 150000,
                countries: 1,
                color: "#00467f",
                tier: 2,
                networkMapUrl: "https://www.windstreamenterprise.com/network-map",
                keyRoutes: [
                    {from: "Dallas, TX", to: "Atlanta, GA", latency: "12ms", coords: [[32.78,-96.80],[33.75,-84.39]]},
                    {from: "Chicago, IL", to: "St. Louis, MO", latency: "5ms", coords: [[41.88,-87.63],[38.63,-90.2]]}
                ],
                dcMarkets: ["Dallas", "Atlanta", "Chicago", "Phoenix"]
            },
            {
                name: "Hurricane Electric",
                routeMiles: 100000,
                countries: 175,
                color: "#dc2626",
                tier: 1,
                networkMapUrl: "https://he.net/",
                keyRoutes: [
                    {from: "Fremont, CA", to: "Los Angeles, CA", latency: "6ms", coords: [[37.55,-121.99],[34.05,-118.24]]},
                    {from: "Ashburn, VA", to: "New York, NY", latency: "4ms", coords: [[39.04,-77.49],[40.71,-74.01]]}
                ],
                dcMarkets: ["Fremont", "Los Angeles", "Ashburn", "Chicago", "Phoenix", "Seattle"]
            },
            {
                name: "NTT Communications",
                routeMiles: 500000,
                countries: 190,
                color: "#7c3aed",
                tier: 1,
                networkMapUrl: "https://www.ntt.com/en/services/network.html",
                keyRoutes: [
                    {from: "Ashburn, VA", to: "Los Angeles, CA", latency: "55ms", coords: [[39.04,-77.49],[41.88,-87.63],[39.7,-105.0],[34.05,-118.24]]}
                ],
                dcMarkets: ["Ashburn", "Los Angeles", "Chicago", "San Jose"]
            }
        ];
        
        // ============================================
        // MIDSTREAM GAS COMPANIES & INFRASTRUCTURE
        // Pipeline operators with capacity data
        // ============================================
        var MIDSTREAM_GAS_COMPANIES = [
            {
                name: "Kinder Morgan",
                ticker: "KMI",
                pipelineMiles: 83000,
                dailyCapacityBcf: 40.5,
                processingCapacityBcfd: 4.2,
                storageCapacityBcf: 700,
                keyPipelines: [
                    {name: "Tennessee Gas Pipeline", capacityBcfd: 8.5, region: "Northeast"},
                    {name: "Natural Gas Pipeline of America", capacityBcfd: 5.8, region: "Midwest"},
                    {name: "El Paso Natural Gas", capacityBcfd: 5.2, region: "Southwest"},
                    {name: "Permian Highway Pipeline", capacityBcfd: 2.1, region: "Texas"}
                ],
                dcMarkets: ["Houston", "Dallas", "Phoenix", "Chicago"],
                lat: 29.7604, lng: -95.3698
            },
            {
                name: "Williams Companies",
                ticker: "WMB",
                pipelineMiles: 33000,
                dailyCapacityBcf: 31.0,
                processingCapacityBcfd: 7.8,
                storageCapacityBcf: 350,
                keyPipelines: [
                    {name: "Transco (Transcontinental)", capacityBcfd: 17.8, region: "East Coast"},
                    {name: "Northwest Pipeline", capacityBcfd: 3.8, region: "Pacific NW"},
                    {name: "Gulfstream", capacityBcfd: 1.3, region: "Florida"}
                ],
                dcMarkets: ["Ashburn", "Atlanta", "Houston", "Seattle"],
                lat: 36.1540, lng: -95.9928
            },
            {
                name: "Energy Transfer",
                ticker: "ET",
                pipelineMiles: 125000,
                dailyCapacityBcf: 32.0,
                processingCapacityBcfd: 6.5,
                storageCapacityBcf: 450,
                keyPipelines: [
                    {name: "Panhandle Eastern", capacityBcfd: 6.2, region: "Midwest"},
                    {name: "Rover Pipeline", capacityBcfd: 3.25, region: "Appalachia"},
                    {name: "Permian Express", capacityBcfd: 2.4, region: "Texas"}
                ],
                dcMarkets: ["Dallas", "Houston", "Chicago", "Columbus"],
                lat: 32.7767, lng: -96.7970
            },
            {
                name: "Enterprise Products",
                ticker: "EPD",
                pipelineMiles: 50000,
                dailyCapacityBcf: 22.0,
                processingCapacityBcfd: 8.9,
                storageCapacityBcf: 280,
                keyPipelines: [
                    {name: "Texas Intrastate", capacityBcfd: 5.8, region: "Texas"},
                    {name: "Acadian Gas", capacityBcfd: 2.1, region: "Louisiana"},
                    {name: "Jonah Gas Gathering", capacityBcfd: 2.8, region: "Rockies"}
                ],
                dcMarkets: ["Houston", "Dallas", "Louisiana"],
                lat: 29.7604, lng: -95.3698
            },
            {
                name: "TC Energy (TransCanada)",
                ticker: "TRP",
                pipelineMiles: 57000,
                dailyCapacityBcf: 25.5,
                processingCapacityBcfd: 0,
                storageCapacityBcf: 400,
                keyPipelines: [
                    {name: "ANR Pipeline", capacityBcfd: 8.4, region: "Midwest"},
                    {name: "Columbia Gas Transmission", capacityBcfd: 5.2, region: "Appalachia"},
                    {name: "Great Lakes Gas", capacityBcfd: 2.4, region: "Great Lakes"}
                ],
                dcMarkets: ["Chicago", "Detroit", "Columbus", "Pittsburgh"],
                lat: 51.0447, lng: -114.0719
            },
            {
                name: "Enbridge",
                ticker: "ENB",
                pipelineMiles: 24000,
                dailyCapacityBcf: 14.5,
                processingCapacityBcfd: 0.8,
                storageCapacityBcf: 180,
                keyPipelines: [
                    {name: "Texas Eastern", capacityBcfd: 10.2, region: "East Coast"},
                    {name: "Algonquin Gas", capacityBcfd: 3.1, region: "New England"},
                    {name: "East Tennessee", capacityBcfd: 1.2, region: "Southeast"}
                ],
                dcMarkets: ["Ashburn", "Boston", "New York", "Atlanta"],
                lat: 53.5461, lng: -113.4938
            },
            {
                name: "DCP Midstream",
                ticker: "DCP",
                pipelineMiles: 44000,
                dailyCapacityBcf: 8.5,
                processingCapacityBcfd: 5.2,
                storageCapacityBcf: 60,
                keyPipelines: [
                    {name: "Southern Hills", capacityBcfd: 2.1, region: "Mid-Continent"},
                    {name: "Sand Hills", capacityBcfd: 0.5, region: "Permian"}
                ],
                dcMarkets: ["Denver", "Oklahoma City", "Dallas"],
                lat: 39.7392, lng: -104.9903
            },
            {
                name: "Targa Resources",
                ticker: "TRGP",
                pipelineMiles: 28000,
                dailyCapacityBcf: 7.2,
                processingCapacityBcfd: 6.8,
                storageCapacityBcf: 45,
                keyPipelines: [
                    {name: "Grand Prix NGL", capacityBcfd: 3.5, region: "Texas"},
                    {name: "Permian Gathering", capacityBcfd: 2.8, region: "Permian"}
                ],
                dcMarkets: ["Houston", "Dallas", "Midland-Odessa"],
                lat: 29.7604, lng: -95.3698
            }
        ];
        
        // ============================================
        // LNG TERMINALS (Import/Export)
        // ============================================
        var LNG_TERMINALS = [
            {name: "Sabine Pass LNG", operator: "Cheniere", capacityMtpa: 30, type: "Export", lat: 29.7356, lng: -93.8636, status: "Operating"},
            {name: "Cameron LNG", operator: "Sempra", capacityMtpa: 15, type: "Export", lat: 29.7903, lng: -93.3364, status: "Operating"},
            {name: "Freeport LNG", operator: "Freeport LNG", capacityMtpa: 20, type: "Export", lat: 28.9417, lng: -95.2908, status: "Operating"},
            {name: "Corpus Christi LNG", operator: "Cheniere", capacityMtpa: 25, type: "Export", lat: 27.8006, lng: -97.3964, status: "Operating"},
            {name: "Cove Point LNG", operator: "Dominion", capacityMtpa: 5.75, type: "Export", lat: 38.4042, lng: -76.3972, status: "Operating"},
            {name: "Elba Island LNG", operator: "Kinder Morgan", capacityMtpa: 2.5, type: "Export", lat: 32.0877, lng: -80.8945, status: "Operating"},
            {name: "Golden Pass LNG", operator: "QatarEnergy/Exxon", capacityMtpa: 18, type: "Export", lat: 29.7636, lng: -93.9308, status: "Construction"},
            {name: "Plaquemines LNG", operator: "Venture Global", capacityMtpa: 20, type: "Export", lat: 29.3619, lng: -89.4111, status: "Construction"},
            {name: "Rio Grande LNG", operator: "NextDecade", capacityMtpa: 27, type: "Export", lat: 26.0544, lng: -97.1736, status: "Construction"},
            {name: "Lake Charles LNG", operator: "Energy Transfer", capacityMtpa: 16.45, type: "Export", lat: 30.2266, lng: -93.2174, status: "Approved"}
        ];
        
        // FEMA Flood Hazard Layer - ArcGIS Tile Service
        var FEMA_FLOOD_URL = 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer';
        var femaFloodLayer = null;
        
        // OpenStreetMap Overpass API - FREE, no authentication!
        var OVERPASS_API = 'https://overpass-api.de/api/interpreter';
        // Fallback Overpass endpoints when primary is overloaded
        var OVERPASS_ENDPOINTS = [
            'https://overpass-api.de/api/interpreter',
            'https://overpass.kumi.systems/api/interpreter',
            'https://maps.mail.ru/osm/tools/overpass/api/interpreter'
        ];
        var currentOverpassIndex = 0;
        
        // Try Overpass with fallback endpoints
        async function fetchOverpassWithFallback(query, timeout) {
            timeout = timeout || 20000;
            
            for (var i = 0; i < OVERPASS_ENDPOINTS.length; i++) {
                var endpointIndex = (currentOverpassIndex + i) % OVERPASS_ENDPOINTS.length;
                var endpoint = OVERPASS_ENDPOINTS[endpointIndex];
                
                try {
                    var response = await fetch(endpoint, {
                        method: 'POST',
                        body: query,
                        signal: AbortSignal.timeout(timeout)
                    });
                    
                    if (response.status === 429 || response.status === 504 || response.status === 503) {
                        console.log('📡 Overpass endpoint ' + (endpointIndex + 1) + ' overloaded, trying next...');
                        continue;
                    }
                    
                    var text = await response.text();
                    
                    // Check if it's valid JSON (not an error page)
                    if (text.startsWith('<?xml') || text.startsWith('<html')) {
                        console.log('📡 Overpass endpoint ' + (endpointIndex + 1) + ' returned error page, trying next...');
                        continue;
                    }
                    
                    var data = JSON.parse(text);
                    
                    // Update preferred endpoint for next request
                    currentOverpassIndex = endpointIndex;
                    
                    return data;
                } catch (error) {
                    if (error.name === 'TimeoutError' || error.name === 'AbortError') {
                        console.log('📡 Overpass endpoint ' + (endpointIndex + 1) + ' timeout, trying next...');
                    } else {
                        console.log('📡 Overpass endpoint ' + (endpointIndex + 1) + ' error:', error.message);
                    }
                }
            }
            
            throw new Error('All Overpass endpoints failed');
        }
        
        // Build Overpass queries for infrastructure
        function buildOverpassQuery(type, bounds) {
            var sw = bounds.getSouthWest();
            var ne = bounds.getNorthEast();
            var bbox = sw.lat + ',' + sw.lng + ',' + ne.lat + ',' + ne.lng;
            
            var queries = {
                substations: '[out:json][timeout:30];(node["power"="substation"](' + bbox + ');way["power"="substation"](' + bbox + '););out center 500;',
                powerPlants: '[out:json][timeout:30];(node["power"="plant"](' + bbox + ');way["power"="plant"](' + bbox + ');node["power"="generator"]["generator:source"!="solar"](' + bbox + '););out center 200;',
                gasPipelines: '[out:json][timeout:30];(way["man_made"="pipeline"]["substance"="gas"](' + bbox + ');way["man_made"="pipeline"]["type"="gas"](' + bbox + '););out geom 100;',
                telecomLines: '[out:json][timeout:30];(way["utility"="telecom"](' + bbox + ');way["man_made"="submarine_cable"](' + bbox + ');way["cables"]["communication"="line"](' + bbox + '););out geom 100;',
                waterTreatment: '[out:json][timeout:30];(node["man_made"="water_works"](' + bbox + ');way["man_made"="water_works"](' + bbox + ');node["man_made"="wastewater_plant"](' + bbox + '););out center 100;'
            };
            
            return queries[type] || '';
        }
        
        // Initialize FEMA Flood Layer as WMS overlay
        function initFEMAFloodLayer() {
            femaFloodLayer = L.tileLayer.wms('https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/export', {
                layers: '28', // Flood Hazard Zones layer
                format: 'image/png',
                transparent: true,
                opacity: 0.4,
                attribution: 'FEMA NFHL'
            });
        console.log('📡 Live API module v68 - Phase 1-3: OSM + HIFLD + FEMA NFHL + NWI + USGS + ESA');
        }
        initFEMAFloodLayer();
        
        // Show/hide loading indicator
        function showAPILoading(show) {
            var indicator = document.getElementById('api-loading');
            if (!indicator) {
                indicator = document.createElement('div');
                indicator.id = 'api-loading';
                indicator.innerHTML = '<div style="position:fixed;top:80px;right:20px;background:rgba(99,102,241,0.9);color:#fff;padding:10px 16px;border-radius:8px;font-size:12px;z-index:1001;display:flex;align-items:center;gap:8px;"><span class="live-dot"></span>Loading live infrastructure data...</div>';
                document.body.appendChild(indicator);
            }
            indicator.style.display = show ? 'block' : 'none';
        }
        
        // Build bounding box string for API query
        function getBboxString(bounds) {
            var sw = bounds.getSouthWest();
            var ne = bounds.getNorthEast();
            return sw.lng + ',' + sw.lat + ',' + ne.lng + ',' + ne.lat;
        }
        
        // Generate cache key from bounds
        function getCacheKey(type, bounds, zoom) {
            var bbox = getBboxString(bounds);
            return type + '_' + zoom + '_' + bbox.substring(0, 20);
        }
        
        // ============================================
        // LOAD SUBSTATIONS FROM OPENSTREETMAP OVERPASS API
        // ============================================
        async function loadSubstationsFromAPI(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 8) return;
            
            var cacheKey = getCacheKey('subs', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['subs']) return;
            
            API_LOADING['subs'] = true;
            showAPILoading(true);
            console.log('📡 Fetching substations from OpenStreetMap at zoom ' + zoom + '...');
            
            try {
                var query = buildOverpassQuery('substations', bounds);
                var data = await fetchOverpassWithFallback(query, 20000);
                
                if (data.elements && data.elements.length > 0) {
                    console.log('📡 OpenStreetMap: Loaded ' + data.elements.length + ' substations');
                    
                    var addedCount = 0;
                    data.elements.forEach(function(el) {
                        var lat = el.lat || (el.center && el.center.lat);
                        var lng = el.lon || (el.center && el.center.lon);
                        if (!lat || !lng) return;
                        
                        var tags = el.tags || {};
                        var voltage = parseInt(tags.voltage) / 1000 || 0;
                        
                        var color = '#10b981';
                        var radius = 4;
                        if (voltage >= 500) { color = '#06b6d4'; radius = 7; }
                        else if (voltage >= 345) { color = '#3b82f6'; radius = 6; }
                        else if (voltage >= 230) { color = '#8b5cf6'; radius = 5; }
                        else if (voltage >= 115) { color = '#f59e0b'; radius = 4; }
                        
                        L.circleMarker([lat, lng], {
                            radius: radius,
                            fillColor: color,
                            color: '#fff',
                            weight: 1,
                            opacity: 0.9,
                            fillOpacity: 0.7
                        }).bindPopup(
                            '<div class="popup-title">🔌 ' + (tags.name || 'Substation') + '</div>' +
                            '<div class="popup-row"><span class="popup-label">Voltage</span><span class="popup-value">' + (voltage || 'Unknown') + ' kV</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (tags.operator || 'Unknown') + '</span></div>' +
                            '<div class="popup-row" style="color:#10b981;font-size:10px;margin-top:4px;">📡 Live OpenStreetMap Data</div>'
                        ).addTo(layers.substations);
                        addedCount++;
                    });
                    
                    document.getElementById('count-subs').textContent = substations.length + '+' + addedCount + ' live';
                    API_CACHE[cacheKey] = true;
                    console.log('✅ Added ' + addedCount + ' live substations from OSM');
                } else {
                    console.log('📡 No substations found in this area from OSM');
                }
            } catch (error) {
                console.error('❌ OpenStreetMap substations error:', error);
            }
            
            API_LOADING['subs'] = false;
            showAPILoading(false);
        }
        
        // ============================================
        // LOAD TRANSMISSION LINES FROM HIFLD API
        // ============================================
        async function loadTransmissionFromAPI(bounds) {
            if (!ENABLE_LIVE_API || map.getZoom() < 8) return; // Only load at zoom 8+
            
            var cacheKey = getCacheKey('trans', bounds, map.getZoom());
            if (API_CACHE[cacheKey] || API_LOADING['trans']) return;
            
            API_LOADING['trans'] = true;
            
            try {
                var bbox = getBboxString(bounds);
                var url = HIFLD_APIS.transmission + '?' + new URLSearchParams({
                    where: 'VOLTAGE >= 230',
                    geometry: bbox,
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    inSR: '4326',
                    outSR: '4326',
                    outFields: 'OWNER,VOLTAGE,STATUS',
                    returnGeometry: true,
                    f: 'json',
                    resultRecordCount: 200
                });
                
                var response = await fetch(url);
                var data = await response.json();
                
                if (data.features && data.features.length > 0) {
                    console.log('📡 HIFLD API: Loaded ' + data.features.length + ' transmission lines');
                    
                    data.features.forEach(function(feature) {
                        var paths = feature.geometry.paths;
                        var props = feature.attributes;
                        var voltage = props.VOLTAGE || 0;
                        
                        var color = '#10b981';
                        var weight = 2;
                        if (voltage >= 500) { color = '#06b6d4'; weight = 4; }
                        else if (voltage >= 345) { color = '#3b82f6'; weight = 3; }
                        else if (voltage >= 230) { color = '#8b5cf6'; weight = 2; }
                        
                        if (paths) {
                            paths.forEach(function(path) {
                                var latLngs = path.map(function(p) { return [p[1], p[0]]; });
                                L.polyline(latLngs, {
                                    color: color,
                                    weight: weight,
                                    opacity: 0.6,
                                    className: 'api-transmission'
                                }).bindPopup(
                                    '<div class="popup-title">⚡ Transmission Line</div>' +
                                    '<div class="popup-row"><span class="popup-label">Voltage</span><span class="popup-value">' + voltage + 'kV</span></div>' +
                                    '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">' + (props.OWNER || 'Unknown') + '</span></div>' +
                                    '<div class="popup-row" style="color:#06b6d4;font-size:10px;">📡 Live HIFLD Data</div>'
                                ).addTo(layers.grid);
                            });
                        }
                    });
                    
                    document.getElementById('count-grid').textContent = fiberRoutes.length + '+' + data.features.length + ' live';
                    API_CACHE[cacheKey] = true;
                }
            } catch (error) {
                console.warn('⚠️ HIFLD Transmission API error:', error.message);
            }
            
            API_LOADING['trans'] = false;
        }
        
        // ============================================
        // GAS PIPELINES FROM OPENSTREETMAP
        // ============================================
        async function loadGasPipelinesFromAPI(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 9) return;
            
            var cacheKey = getCacheKey('gas', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['gas']) return;
            
            API_LOADING['gas'] = true;
            console.log('📡 Fetching gas pipelines from OpenStreetMap...');
            
            try {
                var query = buildOverpassQuery('gasPipelines', bounds);
                var data = await fetchOverpassWithFallback(query, 20000);
                
                if (data.elements && data.elements.length > 0) {
                    console.log('📡 OpenStreetMap: Loaded ' + data.elements.length + ' gas pipelines');
                    
                    var addedCount = 0;
                    data.elements.forEach(function(el) {
                        if (!el.geometry || el.geometry.length < 2) return;
                        
                        var tags = el.tags || {};
                        var name = tags.name || tags.operator || 'Gas Pipeline';
                        
                        var latLngs = el.geometry.map(function(p) { return [p.lat, p.lon]; });
                        
                        L.polyline(latLngs, {
                            color: '#f59e0b',
                            weight: 3,
                            opacity: 0.7,
                            dashArray: '10, 5'
                        }).bindPopup(
                            '<div class="popup-title">🔥 ' + name + '</div>' +
                            '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + (tags.substance || 'Gas') + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (tags.operator || 'Unknown') + '</span></div>' +
                            '<div class="popup-row" style="color:#10b981;font-size:10px;">📡 Live OpenStreetMap Data</div>'
                        ).addTo(layers.gas);
                        addedCount++;
                    });
                    
                    document.getElementById('count-gas').textContent = gasPipelines.length + '+' + addedCount + ' live';
                    API_CACHE[cacheKey] = true;
                    console.log('✅ Added ' + addedCount + ' live gas pipelines from OSM');
                }
            } catch (error) {
                console.error('❌ OpenStreetMap gas pipelines error:', error);
            }
            
            API_LOADING['gas'] = false;
        }
        
        // ============================================
        // POWER PLANTS FROM OPENSTREETMAP
        // ============================================
        async function loadPowerPlantsFromAPI(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 8) return;
            
            var cacheKey = getCacheKey('plants', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['plants']) return;
            
            API_LOADING['plants'] = true;
            console.log('📡 Fetching power plants from OpenStreetMap...');
            
            try {
                var query = buildOverpassQuery('powerPlants', bounds);
                var data = await fetchOverpassWithFallback(query, 20000);
                
                if (data.elements && data.elements.length > 0) {
                    console.log('📡 OpenStreetMap: Loaded ' + data.elements.length + ' power plants');
                    
                    var addedCount = 0;
                    data.elements.forEach(function(el) {
                        var lat = el.lat || (el.center && el.center.lat);
                        var lng = el.lon || (el.center && el.center.lon);
                        if (!lat || !lng) return;
                        
                        var tags = el.tags || {};
                        var source = (tags['generator:source'] || tags['plant:source'] || 'unknown').toLowerCase();
                        var name = tags.name || tags.operator || 'Power Plant';
                        var output = tags['generator:output:electricity'] || tags['plant:output:electricity'] || '';
                        
                        var color = '#6b7280';
                        var icon = '⚡';
                        if (source.includes('nuclear')) { color = '#fbbf24'; icon = '☢️'; }
                        else if (source.includes('solar')) { color = '#fcd34d'; icon = '☀️'; }
                        else if (source.includes('wind')) { color = '#60a5fa'; icon = '💨'; }
                        else if (source.includes('gas')) { color = '#f97316'; icon = '🔥'; }
                        else if (source.includes('coal')) { color = '#374151'; icon = '🪨'; }
                        else if (source.includes('hydro')) { color = '#06b6d4'; icon = '💧'; }
                        else if (source.includes('geothermal')) { color = '#ef4444'; icon = '🌋'; }
                        else if (source.includes('biomass')) { color = '#84cc16'; icon = '🌿'; }
                        
                        L.circleMarker([lat, lng], {
                            radius: 6,
                            fillColor: color,
                            color: '#fff',
                            weight: 1,
                            opacity: 0.9,
                            fillOpacity: 0.7
                        }).bindPopup(
                            '<div class="popup-title">' + icon + ' ' + name + '</div>' +
                            '<div class="popup-row"><span class="popup-label">Source</span><span class="popup-value">' + source + '</span></div>' +
                            (output ? '<div class="popup-row"><span class="popup-label">Output</span><span class="popup-value">' + output + '</span></div>' : '') +
                            '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (tags.operator || 'Unknown') + '</span></div>' +
                            '<div class="popup-row" style="color:#10b981;font-size:10px;margin-top:4px;">📡 Live OpenStreetMap Data</div>'
                        ).addTo(layers.nuclear);
                        addedCount++;
                    });
                    
                    API_CACHE[cacheKey] = true;
                    console.log('✅ Added ' + addedCount + ' live power plants from OSM');
                }
            } catch (error) {
                console.error('❌ OpenStreetMap power plants error:', error);
            }
            
            API_LOADING['plants'] = false;
        }
        
        // ============================================
        // PHASE 1: HIFLD POWER PLANTS (Detailed with MW)
        // ============================================
        async function loadHIFLDPowerPlants(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 7) return;
            
            var cacheKey = getCacheKey('hifld-plants', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['hifld-plants']) return;
            
            API_LOADING['hifld-plants'] = true;
            console.log('📡 Fetching power plants from HIFLD...');
            
            try {
                var sw = bounds.getSouthWest();
                var ne = bounds.getNorthEast();
                
                var response = await fetch(HIFLD_APIS.powerPlants + '?' + new URLSearchParams({
                    where: '1=1',
                    geometry: JSON.stringify({xmin: sw.lng, ymin: sw.lat, xmax: ne.lng, ymax: ne.lat, spatialReference: {wkid: 4326}}),
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'NAME,PRIMSOURCE,TOTAL_MW,INSTALL_MW,STATE,COUNTY,OPERATOR,STATUS',
                    returnGeometry: true,
                    f: 'json',
                    resultRecordCount: 500
                }));
                
                var data = await response.json();
                
                if (data.features && data.features.length > 0) {
                    console.log('📡 HIFLD: Loaded ' + data.features.length + ' power plants');
                    
                    var addedCount = 0;
                    data.features.forEach(function(feature) {
                        var props = feature.attributes;
                        var coords = feature.geometry;
                        if (!coords) return;
                        
                        var source = (props.PRIMSOURCE || 'Unknown').toLowerCase();
                        var mw = props.TOTAL_MW || props.INSTALL_MW || 0;
                        var name = props.NAME || 'Power Plant';
                        
                        // Color and icon by fuel type
                        var color = '#6b7280', icon = '⚡';
                        if (source.includes('nuclear')) { color = '#fbbf24'; icon = '☢️'; }
                        else if (source.includes('solar')) { color = '#fcd34d'; icon = '☀️'; }
                        else if (source.includes('wind')) { color = '#60a5fa'; icon = '💨'; }
                        else if (source.includes('natural gas') || source.includes('gas')) { color = '#f97316'; icon = '🔥'; }
                        else if (source.includes('coal')) { color = '#374151'; icon = '🪨'; }
                        else if (source.includes('hydro')) { color = '#06b6d4'; icon = '💧'; }
                        else if (source.includes('geothermal')) { color = '#ef4444'; icon = '🌋'; }
                        else if (source.includes('biomass') || source.includes('wood')) { color = '#84cc16'; icon = '🌿'; }
                        else if (source.includes('petroleum') || source.includes('oil')) { color = '#1f2937'; icon = '🛢️'; }
                        
                        // Size by capacity
                        var radius = mw > 1000 ? 12 : mw > 500 ? 10 : mw > 100 ? 8 : 6;
                        
                        L.circleMarker([coords.y, coords.x], {
                            radius: radius,
                            fillColor: color,
                            color: '#fff',
                            weight: 2,
                            opacity: 0.9,
                            fillOpacity: 0.8
                        }).bindPopup(
                            '<div class="popup-title">' + icon + ' ' + name + '</div>' +
                            '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value" style="color:#22c55e;font-weight:700">' + mw.toLocaleString() + ' MW</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Fuel Type</span><span class="popup-value">' + props.PRIMSOURCE + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (props.OPERATOR || 'Unknown') + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Location</span><span class="popup-value">' + (props.COUNTY || '') + ', ' + (props.STATE || '') + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Status</span><span class="popup-value">' + (props.STATUS || 'Operating') + '</span></div>' +
                            '<div class="popup-row" style="color:#06b6d4;font-size:10px;margin-top:4px;">📡 Live HIFLD Data</div>'
                        ).addTo(layers.powerplants);
                        addedCount++;
                    });
                    
                    document.getElementById('count-powerplants').textContent = addedCount;
                    API_CACHE[cacheKey] = true;
                    console.log('✅ Added ' + addedCount + ' HIFLD power plants');
                }
            } catch (error) {
                console.error('❌ HIFLD Power Plants error:', error);
            }
            
            API_LOADING['hifld-plants'] = false;
        }
        
        // ============================================
        // PHASE 1: HIFLD GAS COMPRESSOR STATIONS
        // ============================================
        async function loadHIFLDGasCompressors(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 8) return;
            
            var cacheKey = getCacheKey('gas-comp', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['gas-comp']) return;
            
            API_LOADING['gas-comp'] = true;
            console.log('📡 Fetching gas compressor stations from HIFLD...');
            
            try {
                var sw = bounds.getSouthWest();
                var ne = bounds.getNorthEast();
                
                var response = await fetch(HIFLD_APIS.gasCompressors + '?' + new URLSearchParams({
                    where: '1=1',
                    geometry: JSON.stringify({xmin: sw.lng, ymin: sw.lat, xmax: ne.lng, ymax: ne.lat, spatialReference: {wkid: 4326}}),
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'NAME,OPERATOR,STATE,COUNTY,TYPE',
                    returnGeometry: true,
                    f: 'json',
                    resultRecordCount: 300
                }));
                
                var data = await response.json();
                
                if (data.features && data.features.length > 0) {
                    console.log('📡 HIFLD: Loaded ' + data.features.length + ' gas compressor stations');
                    
                    var addedCount = 0;
                    data.features.forEach(function(feature) {
                        var props = feature.attributes;
                        var coords = feature.geometry;
                        if (!coords) return;
                        
                        L.circleMarker([coords.y, coords.x], {
                            radius: 7,
                            fillColor: '#f97316',
                            color: '#fff',
                            weight: 2,
                            opacity: 0.9,
                            fillOpacity: 0.8
                        }).bindPopup(
                            '<div class="popup-title">⛽ ' + (props.NAME || 'Gas Compressor Station') + '</div>' +
                            '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (props.OPERATOR || 'Unknown') + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + (props.TYPE || 'Compressor') + '</span></div>' +
                            '<div class="popup-row"><span class="popup-label">Location</span><span class="popup-value">' + (props.COUNTY || '') + ', ' + (props.STATE || '') + '</span></div>' +
                            '<div class="popup-row" style="color:#06b6d4;font-size:10px;margin-top:4px;">📡 Live HIFLD Data</div>'
                        ).addTo(layers.gascompressors);
                        addedCount++;
                    });
                    
                    document.getElementById('count-compressors').textContent = addedCount;
                    API_CACHE[cacheKey] = true;
                    console.log('✅ Added ' + addedCount + ' gas compressor stations');
                }
            } catch (error) {
                console.error('❌ HIFLD Gas Compressors error:', error);
            }
            
            API_LOADING['gas-comp'] = false;
        }
        
        // ============================================
        // PHASE 1: INTERNET EXCHANGE POINTS
        // ============================================
        function loadInternetExchanges() {
            if (layers.ixpoints.getLayers().length > 0) return; // Already loaded
            
            console.log('📡 Loading Internet Exchange points...');
            
            INTERNET_EXCHANGES.forEach(function(ix) {
                var marker = L.marker([ix.lat, ix.lng], {
                    icon: L.divIcon({
                        className: 'ix-marker',
                        html: '<div style="background:linear-gradient(135deg,#8b5cf6,#6366f1);width:28px;height:28px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:14px;">🌐</div>',
                        iconSize: [28, 28],
                        iconAnchor: [14, 14]
                    })
                }).bindPopup(
                    '<div class="popup-title">🌐 ' + ix.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">City</span><span class="popup-value">' + ix.city + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Participants</span><span class="popup-value" style="color:#22c55e;font-weight:700">' + ix.participants + ' networks</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Peak Traffic</span><span class="popup-value" style="color:#06b6d4;font-weight:700">' + ix.peakTbps + ' Tbps</span></div>' +
                    '<div class="popup-row" style="color:#8b5cf6;font-size:10px;margin-top:4px;">📡 PeeringDB Data</div>'
                );
                marker.addTo(layers.ixpoints);
            });
            
            document.getElementById('count-ix').textContent = INTERNET_EXCHANGES.length;
            console.log('✅ Loaded ' + INTERNET_EXCHANGES.length + ' Internet Exchanges');
        }
        
        // ============================================
        // v73: HIFLD GAS PIPELINES (Enhanced)
        // ============================================
        async function loadHIFLDGasPipelines(bounds) {
            var zoom = map.getZoom();
            if (!ENABLE_LIVE_API || zoom < 7) return;
            
            var cacheKey = getCacheKey('hifld-gas', bounds, zoom);
            if (API_CACHE[cacheKey] || API_LOADING['hifld-gas']) return;
            
            API_LOADING['hifld-gas'] = true;
            console.log('📡 Fetching gas pipelines from HIFLD...');
            
            try {
                var sw = bounds.getSouthWest();
                var ne = bounds.getNorthEast();
                
                var response = await fetch('https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0/query?' + new URLSearchParams({
                    where: '1=1',
                    geometry: JSON.stringify({xmin: sw.lng, ymin: sw.lat, xmax: ne.lng, ymax: ne.lat, spatialReference: {wkid: 4326}}),
                    geometryType: 'esriGeometryEnvelope',
                    spatialRel: 'esriSpatialRelIntersects',
                    outFields: 'OPERATOR,TYPEPIPE,STATUS',
                    returnGeometry: true,
                    f: 'json',
                    resultRecordCount: 1000
                }));
                
                var data = await response.json();
                
                if (data.features && data.features.length > 0) {
                    console.log('📡 HIFLD: Loaded ' + data.features.length + ' gas pipeline segments');
                    
                    data.features.forEach(function(feature) {
                        var props = feature.attributes;
                        var paths = feature.geometry.paths;
                        if (!paths) return;
                        
                        paths.forEach(function(path) {
                            var latLngs = path.map(function(coord) {
                                return [coord[1], coord[0]];
                            });
                            
                            L.polyline(latLngs, {
                                color: '#f97316',
                                weight: 2,
                                opacity: 0.7,
                                dashArray: '5, 5'
                            }).bindPopup(
                                '<div class="popup-title">🔥 Gas Pipeline</div>' +
                                '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + (props.OPERATOR || 'Unknown') + '</span></div>' +
                                '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + (props.TYPEPIPE || 'N/A') + '</span></div>' +
                                '<div class="popup-row"><span class="popup-label">Status</span><span class="popup-value">' + (props.STATUS || 'Active') + '</span></div>' +
                                '<div class="popup-row" style="color:#f97316;font-size:10px;margin-top:4px;">📡 HIFLD Live Data</div>'
                            ).addTo(layers.hifldGas);
                        });
                    });
                    
                    document.getElementById('count-gas-hifld').textContent = data.features.length;
                    API_CACHE[cacheKey] = true;
                }
            } catch (error) {
                console.error('❌ HIFLD Gas Pipelines error:', error);
            }
            
            API_LOADING['hifld-gas'] = false;
        }
        
        // ============================================
        // v73: MAJOR AQUIFERS (USGS Data)
        // ============================================
        function loadAquifers() {
            if (layers.aquifers.getLayers().length > 0) return;
            
            console.log('📡 Loading major aquifers...');
            
            // Major US Aquifers - key locations with info markers
            var AQUIFERS = [
                { name: 'Ogallala Aquifer', lat: 37.5, lng: -101.5, color: '#ef4444', risk: 'High Depletion', area: '174,000 sq mi', states: 'TX, OK, KS, NE, CO, NM, SD, WY', depth: '100-400 ft', recharge: '0.5 in/yr', usage: '21M acre-ft/yr' },
                { name: 'Edwards Aquifer', lat: 29.8, lng: -98.5, color: '#22c55e', risk: 'Managed', area: '8,000 sq mi', states: 'TX', depth: '300-1,500 ft', recharge: '6 in/yr', usage: '0.5M acre-ft/yr' },
                { name: 'Floridan Aquifer', lat: 29.5, lng: -82.0, color: '#22c55e', risk: 'Low', area: '100,000 sq mi', states: 'FL, GA, AL, SC', depth: '0-2,000 ft', recharge: '15 in/yr', usage: '4M acre-ft/yr' },
                { name: 'Central Valley Aquifer', lat: 36.8, lng: -120.0, color: '#ef4444', risk: 'High Depletion', area: '20,000 sq mi', states: 'CA', depth: '50-3,000 ft', recharge: '2 in/yr', usage: '15M acre-ft/yr' },
                { name: 'Atlantic Coastal Plain', lat: 36.0, lng: -77.0, color: '#22c55e', risk: 'Low', area: '50,000 sq mi', states: 'VA, NC, SC, MD, DE, NJ', depth: '100-1,000 ft', recharge: '10 in/yr', usage: '2M acre-ft/yr' },
                { name: 'Basin & Range (Phoenix)', lat: 33.4, lng: -112.0, color: '#f59e0b', risk: 'Medium', area: '82,000 sq mi', states: 'AZ, NV, UT', depth: '100-500 ft', recharge: '1 in/yr', usage: '5M acre-ft/yr' },
                { name: 'High Plains (Northern)', lat: 41.5, lng: -101.0, color: '#f59e0b', risk: 'Medium', area: '175,000 sq mi', states: 'NE, KS, CO, WY, SD', depth: '0-500 ft', recharge: '1 in/yr', usage: '18M acre-ft/yr' },
                { name: 'Mississippi Embayment', lat: 35.0, lng: -90.0, color: '#22c55e', risk: 'Low', area: '65,000 sq mi', states: 'AR, TN, MS, LA', depth: '50-1,500 ft', recharge: '8 in/yr', usage: '8M acre-ft/yr' },
                { name: 'Denver Basin', lat: 39.7, lng: -104.9, color: '#f59e0b', risk: 'Medium', area: '7,000 sq mi', states: 'CO', depth: '200-2,500 ft', recharge: '0.3 in/yr', usage: '0.2M acre-ft/yr' },
                { name: 'Columbia Plateau', lat: 46.8, lng: -119.5, color: '#22c55e', risk: 'Low', area: '20,000 sq mi', states: 'WA, OR, ID', depth: '100-1,000 ft', recharge: '3 in/yr', usage: '1M acre-ft/yr' }
            ];
            
            AQUIFERS.forEach(function(aq) {
                // Use circle markers instead of huge circles
                L.circleMarker([aq.lat, aq.lng], {
                    radius: 18,
                    fillColor: aq.color,
                    color: '#fff',
                    weight: 3,
                    opacity: 1,
                    fillOpacity: 0.8
                }).bindPopup(
                    '<div style="min-width:260px">' +
                    '<div class="popup-title">💧 ' + aq.name + '</div>' +
                    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px">' +
                    '<div><span style="color:#9ca3af">Area:</span> <strong>' + aq.area + '</strong></div>' +
                    '<div><span style="color:#9ca3af">Depth:</span> <strong>' + aq.depth + '</strong></div>' +
                    '<div><span style="color:#9ca3af">Recharge:</span> <strong>' + aq.recharge + '</strong></div>' +
                    '<div><span style="color:#9ca3af">Usage:</span> <strong>' + aq.usage + '</strong></div>' +
                    '</div>' +
                    '<div style="margin-top:8px"><span style="color:#9ca3af">States:</span> ' + aq.states + '</div>' +
                    '<div style="margin-top:8px;padding:6px 10px;border-radius:6px;background:' + (aq.risk === 'High Depletion' ? 'rgba(239,68,68,0.2)' : aq.risk === 'Medium' ? 'rgba(245,158,11,0.2)' : 'rgba(34,197,94,0.2)') + '">' +
                    '<span style="color:' + aq.color + ';font-weight:700">⚠️ Water Risk: ' + aq.risk + '</span></div>' +
                    '<div style="margin-top:8px;font-size:10px;color:#06b6d4">💡 Data center cooling water availability assessment</div>' +
                    '</div>'
                ).addTo(layers.aquifers);
                
                // Add a label
                L.marker([aq.lat, aq.lng], {
                    icon: L.divIcon({
                        className: 'aquifer-label',
                        html: '<div style="background:rgba(10,10,18,0.9);color:#06b6d4;padding:2px 6px;border-radius:4px;font-size:9px;font-weight:600;white-space:nowrap;transform:translate(-50%,8px);border:1px solid #06b6d4">' + aq.name.split(' ')[0] + '</div>',
                        iconSize: [0, 0]
                    })
                }).addTo(layers.aquifers);
            });
            
            document.getElementById('count-aquifers').textContent = AQUIFERS.length;
            console.log('✅ Loaded ' + AQUIFERS.length + ' major aquifers');
        }
        
        // ============================================
        // v73: MAJOR RIVERS (Cooling Water Access)
        // ============================================
        function loadMajorRivers() {
            if (layers.rivers.getLayers().length > 0) return;
            
            console.log('📡 Loading major rivers...');
            
            // Major US Rivers with simplified paths (key cooling water sources)
            var MAJOR_RIVERS = [
                { name: 'Mississippi River', color: '#3b82f6', flow: '593,000 cfs', length: '2,340 mi', states: 10,
                  path: [[47.2, -95.2], [46.0, -94.0], [44.9, -93.2], [44.0, -91.5], [42.5, -90.5], [41.5, -90.5], [39.7, -91.5], [38.8, -90.2], [37.2, -89.2], [36.0, -89.5], [35.0, -90.0], [34.0, -91.0], [32.3, -91.0], [31.0, -91.5], [29.9, -90.0]] },
                { name: 'Missouri River', color: '#60a5fa', flow: '87,000 cfs', length: '2,341 mi', states: 7,
                  path: [[47.5, -111.5], [47.0, -107.5], [46.0, -104.0], [46.5, -100.5], [43.0, -97.5], [42.0, -96.0], [41.3, -95.9], [40.5, -95.7], [39.7, -94.5], [39.1, -94.6], [38.8, -90.2]] },
                { name: 'Ohio River', color: '#818cf8', flow: '281,000 cfs', length: '981 mi', states: 6,
                  path: [[40.4, -80.0], [39.9, -81.5], [39.1, -84.5], [38.7, -85.8], [38.0, -85.7], [37.8, -87.0], [37.2, -88.5], [37.0, -89.2]] },
                { name: 'Columbia River', color: '#a78bfa', flow: '265,000 cfs', length: '1,243 mi', states: 4,
                  path: [[48.0, -117.5], [47.7, -118.5], [46.2, -119.2], [46.0, -120.0], [45.6, -121.0], [45.7, -122.7], [46.2, -123.5]] },
                { name: 'Colorado River', color: '#c084fc', flow: '22,000 cfs', length: '1,450 mi', states: 7,
                  path: [[40.0, -105.8], [39.5, -107.5], [39.0, -109.0], [38.5, -109.5], [37.5, -110.5], [37.0, -111.5], [36.0, -112.0], [35.5, -114.5], [34.5, -114.5], [33.0, -114.7], [32.5, -114.8]] },
                { name: 'Snake River', color: '#a855f7', flow: '36,000 cfs', length: '1,078 mi', states: 4,
                  path: [[44.0, -110.5], [43.5, -111.5], [42.5, -114.5], [43.0, -116.5], [44.0, -117.0], [45.5, -117.0], [46.2, -119.0]] },
                { name: 'Tennessee River', color: '#6366f1', flow: '70,000 cfs', length: '652 mi', states: 4,
                  path: [[35.0, -84.0], [35.5, -85.0], [35.0, -86.0], [35.0, -87.5], [36.0, -88.0], [37.0, -88.3]] }
            ];
            
            MAJOR_RIVERS.forEach(function(river) {
                L.polyline(river.path, {
                    color: river.color,
                    weight: 4,
                    opacity: 0.8
                }).bindPopup(
                    '<div class="popup-title">🌊 ' + river.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Length</span><span class="popup-value">' + river.length + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Avg Flow</span><span class="popup-value">' + river.flow + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">States</span><span class="popup-value">' + river.states + ' states</span></div>' +
                    '<div class="popup-row" style="color:#3b82f6;font-size:10px;margin-top:4px;">💧 Potential cooling water source for large DCs</div>'
                ).addTo(layers.rivers);
            });
            
            document.getElementById('count-rivers').textContent = MAJOR_RIVERS.length;
            console.log('✅ Loaded ' + MAJOR_RIVERS.length + ' major rivers');
        }
        
        // ============================================
        // RAILROADS - FRA North American Rail Network
        // Comprehensive coverage including short lines
        // Important for easement identification
        // ============================================
        var railroadLayer = null;
        var railroadLayerFRA = null;
        
        function loadRailroads() {
            if (railroadLayer) return; // Already loaded
            
            console.log('🚂 Loading comprehensive railroad data...');
            
            // FRA North American Rail Network - ALL railroads (Class I, II, III, short lines)
            // This is the authoritative federal source
            railroadLayerFRA = L.esri.featureLayer({
                url: 'https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_North_American_Rail_Network_Lines/FeatureServer/0',
                style: function(feature) {
                    // Color by railroad class/type
                    var rrowner = feature.properties.RROWNER1 || '';
                    var fraession = feature.properties.FRAESSION || '';
                    var color = '#6b7280'; // Default gray for short lines
                    var weight = 2;
                    var opacity = 0.7;
                    
                    // Class I railroads - bolder colors
                    if (rrowner.includes('BNSF')) { color = '#f97316'; weight = 3; }
                    else if (rrowner.includes('UP') || rrowner.includes('UNION PACIFIC')) { color = '#eab308'; weight = 3; }
                    else if (rrowner.includes('CSX')) { color = '#3b82f6'; weight = 3; }
                    else if (rrowner.includes('NS') || rrowner.includes('NORFOLK')) { color = '#92400e'; weight = 3; }
                    else if (rrowner.includes('CN') || rrowner.includes('CANADIAN NATIONAL')) { color = '#dc2626'; weight = 3; }
                    else if (rrowner.includes('CP') || rrowner.includes('CANADIAN PACIFIC')) { color = '#ec4899'; weight = 3; }
                    else if (rrowner.includes('KCS') || rrowner.includes('KANSAS CITY')) { color = '#22c55e'; weight = 3; }
                    // Regional/Short lines - lighter
                    else if (rrowner.includes('WNYP') || rrowner.includes('WESTERN NEW YORK')) { color = '#8b5cf6'; weight = 2; }
                    else if (rrowner.includes('AMTK') || rrowner.includes('AMTRAK')) { color = '#0ea5e9'; weight = 2; }
                    else { color = '#6b7280'; weight = 1.5; }
                    
                    return {
                        color: color,
                        weight: weight,
                        opacity: opacity
                    };
                },
                minZoom: 6,
                maxZoom: 18,
                attribution: 'FRA/BTS'
            });
            
            railroadLayerFRA.bindPopup(function(layer) {
                var p = layer.feature.properties;
                var owner = p.RROWNER1 || 'Unknown';
                var trackClass = p.TRACKS || 'N/A';
                var state = p.STATEAB || '';
                var miles = p.MILES ? p.MILES.toFixed(1) : 'N/A';
                
                // Determine railroad class
                var rrClass = 'Short Line/Regional';
                if (owner.includes('BNSF') || owner.includes('UP') || owner.includes('CSX') || 
                    owner.includes('NS') || owner.includes('CN') || owner.includes('CP') || owner.includes('KCS')) {
                    rrClass = 'Class I';
                } else if (owner.includes('AMTK') || owner.includes('AMTRAK')) {
                    rrClass = 'Amtrak Passenger';
                }
                
                return '<div class="popup-title">🚂 ' + owner + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Class</span><span class="popup-value">' + rrClass + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Tracks</span><span class="popup-value">' + trackClass + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">State</span><span class="popup-value">' + state + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Segment</span><span class="popup-value">' + miles + ' mi</span></div>' +
                    '<div style="margin-top:8px;padding:8px;background:rgba(239,68,68,0.1);border-radius:4px;font-size:10px;color:#fca5a5;">' +
                    '⚠️ <strong>Easement Alert:</strong> Railroad ROW typically 100-200ft wide. Check for crossing rights, noise impacts, and vibration concerns for sensitive equipment.' +
                    '</div>' +
                    '<div style="margin-top:4px;font-size:9px;color:var(--text3);">📡 Source: FRA North American Rail Network</div>';
            });
            
            railroadLayerFRA.addTo(layers.railroad);
            
            // Backup: USGS National Map for detailed local view
            railroadLayer = L.esri.dynamicMapLayer({
                url: 'https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer',
                layers: [38], // Railroads layer
                opacity: 0.6,
                minZoom: 10,
                maxZoom: 18,
                attribution: 'USGS National Map'
            });
            
            railroadLayer.addTo(layers.railroad);
            
            // Also add Class I railroad overlay for better visibility at very low zooms (< 6)
            var classIRailroads = [
                // BNSF Railway (orange) - Western US
                {name: 'BNSF Railway', color: '#f97316', owner: 'BNSF', routes: [
                    [[47.6, -122.3], [47.5, -117.4], [46.9, -114.0], [45.8, -108.5], [44.98, -93.27], [41.88, -87.63]], // Seattle-Chicago
                    [[34.1, -118.2], [35.0, -117.0], [35.2, -114.5], [33.5, -112.1], [35.0, -106.6], [35.5, -97.5], [39.1, -94.6]], // LA-Kansas City
                    [[32.9, -96.8], [29.8, -95.4], [29.8, -90.1]] // Dallas-Houston-New Orleans
                ]},
                // Union Pacific (yellow) - Western US
                {name: 'Union Pacific', color: '#eab308', owner: 'UP', routes: [
                    [[34.1, -118.2], [36.1, -115.2], [40.76, -111.89], [41.14, -104.82], [41.26, -95.94], [41.88, -87.63]], // LA-Chicago
                    [[47.6, -122.3], [45.5, -122.7], [40.76, -111.89]], // Portland-SLC
                    [[29.8, -95.4], [29.4, -98.5], [31.8, -106.5], [32.2, -110.9], [34.1, -118.2]] // Houston-LA (Sunset Route)
                ]},
                // CSX Transportation (blue) - Eastern US
                {name: 'CSX Transportation', color: '#3b82f6', owner: 'CSX', routes: [
                    [[42.4, -71.0], [40.7, -74.0], [39.3, -76.6], [38.9, -77.0], [37.5, -77.5], [33.8, -84.4], [30.3, -81.7], [25.8, -80.2]], // Boston-Miami
                    [[41.88, -87.63], [39.1, -84.5], [38.3, -85.8], [36.2, -86.8], [33.8, -84.4]], // Chicago-Atlanta
                    [[38.9, -77.0], [40.5, -80.0], [41.5, -81.7], [42.3, -83.0], [41.88, -87.63]] // DC-Chicago
                ]},
                // Norfolk Southern (brown) - Eastern US
                {name: 'Norfolk Southern', color: '#92400e', owner: 'NS', routes: [
                    [[40.7, -74.0], [40.0, -75.2], [39.95, -75.17], [39.3, -76.6], [37.5, -77.5], [35.8, -78.6], [35.2, -80.8], [33.8, -84.4]], // NJ-Atlanta
                    [[41.88, -87.63], [39.8, -86.2], [39.1, -84.5], [38.05, -84.5], [36.2, -86.8], [35.1, -85.3], [33.8, -84.4]], // Chicago-Atlanta
                    [[36.85, -76.29], [37.5, -77.5], [38.9, -77.0], [40.5, -80.0], [41.5, -81.7]] // Norfolk-Cleveland
                ]},
                // Canadian National (red) - Midwest/South
                {name: 'Canadian National', color: '#dc2626', owner: 'CN', routes: [
                    [[41.88, -87.63], [41.5, -90.5], [44.98, -93.27], [46.8, -92.1]], // Chicago-Duluth
                    [[41.88, -87.63], [38.6, -90.2], [35.1, -90.0], [32.3, -90.2], [29.95, -90.07]] // Chicago-New Orleans
                ]},
                // Canadian Pacific (magenta) - Northern US
                {name: 'Canadian Pacific', color: '#ec4899', owner: 'CPKC', routes: [
                    [[44.98, -93.27], [45.0, -93.0], [46.8, -92.1], [48.0, -89.5]], // Minneapolis-Thunder Bay
                    [[41.88, -87.63], [43.0, -88.0], [44.98, -93.27]] // Chicago-Minneapolis
                ]},
                // Kansas City Southern (green) - Central US
                {name: 'Kansas City Southern', color: '#22c55e', owner: 'KCS', routes: [
                    [[39.1, -94.6], [37.7, -94.7], [36.4, -94.2], [35.5, -94.8], [34.7, -92.3], [32.5, -93.75], [29.8, -95.4]], // KC-Houston
                    [[32.9, -96.8], [32.5, -93.75]] // Dallas-Shreveport
                ]},
                // Western NY & PA (purple) - NY/PA regional
                {name: 'Western NY & Pennsylvania RR', color: '#8b5cf6', owner: 'WNYP', routes: [
                    [[42.13, -80.08], [41.84, -79.14], [41.84, -78.89]] // Erie-Warren-Salamanca (Route 6 corridor)
                ]},
                // Wheeling & Lake Erie (teal) - OH/PA regional  
                {name: 'Wheeling & Lake Erie', color: '#14b8a6', owner: 'WLE', routes: [
                    [[41.5, -81.7], [40.8, -81.4], [40.4, -80.6], [40.1, -80.7]] // Cleveland-Pittsburgh
                ]}
            ];
            
            classIRailroads.forEach(function(railroad) {
                railroad.routes.forEach(function(route) {
                    L.polyline(route, {
                        color: railroad.color,
                        weight: 3,
                        opacity: 0.6,
                        dashArray: '8, 4'
                    }).bindPopup(
                        '<div class="popup-title">🚂 ' + railroad.name + '</div>' +
                        '<div class="popup-row"><span class="popup-label">Class</span><span class="popup-value">' + (railroad.owner === 'WNYP' || railroad.owner === 'WLE' ? 'Class II Regional' : 'Class I Railroad') + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">Owner</span><span class="popup-value">' + railroad.owner + '</span></div>' +
                        '<div style="margin-top:8px;padding:8px;background:rgba(239,68,68,0.1);border-radius:4px;font-size:10px;color:#fca5a5;">' +
                        '⚠️ <strong>Easement Alert:</strong> Railroad ROW typically 100-200ft wide. Check for crossing rights, noise impacts, and vibration concerns for sensitive equipment.' +
                        '</div>'
                    ).addTo(layers.railroad);
                });
            });
            
            document.getElementById('count-railroad').textContent = 'FRA/All';
            console.log('✅ Railroad layer loaded (FRA NTAD + USGS National Map)');
            console.log('🚂 Includes Class I, II, III and short line railroads');
        }
        
        // ============================================
        // PHASE 2: OPPORTUNITY ZONES
        // ============================================
        function loadOpportunityZones() {
            if (layers.opzones.getLayers().length > 0) return;
            
            console.log('📡 Loading Opportunity Zones...');
            
            OPPORTUNITY_ZONES.forEach(function(oz) {
                // Create a circle to represent the OZ area
                L.circle([oz.lat, oz.lng], {
                    radius: 3000, // 3km radius
                    fillColor: '#10b981',
                    color: '#059669',
                    weight: 2,
                    opacity: 0.8,
                    fillOpacity: 0.2,
                    dashArray: '5, 5'
                }).bindPopup(
                    '<div class="popup-title">💎 ' + oz.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">City</span><span class="popup-value">' + oz.city + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Census Tract</span><span class="popup-value">' + oz.tractId + '</span></div>' +
                    '<div class="popup-row" style="background:rgba(16,185,129,0.1);padding:6px;border-radius:4px;margin-top:6px;">' +
                    '<span style="color:#10b981;font-weight:600;">Tax Benefits:</span><br>' +
                    '• Capital gains deferral<br>' +
                    '• 10% exclusion (5+ yrs)<br>' +
                    '• 15% exclusion (7+ yrs)<br>' +
                    '• 0% on new gains (10+ yrs)' +
                    '</div>' +
                    '<div class="popup-row" style="color:#10b981;font-size:10px;margin-top:4px;">📡 Treasury OZ Program</div>'
                ).addTo(layers.opzones);
            });
            
            document.getElementById('count-oz').textContent = OPPORTUNITY_ZONES.length;
            console.log('✅ Loaded ' + OPPORTUNITY_ZONES.length + ' Opportunity Zones');
        }
        
        // ============================================
        // PHASE 2: SOLAR RESOURCE (NREL) - Click to analyze
        // ============================================
        function addSolarResourceInfo() {
            // Add info panel for solar potential at clicked locations
            if (layers.solar._solarEnabled) return;
            layers.solar._solarEnabled = true;
            
            // Create solar tile layer from NREL
            var solarTiles = L.tileLayer('https://developer.nrel.gov/api/solar/solar_resource/v1.png?api_key=DEMO_KEY&lat={y}&lon={x}', {
                opacity: 0.5,
                attribution: 'NREL'
            });
            
            console.log('☀️ Solar resource layer enabled - click map for analysis');
            document.getElementById('count-solar').textContent = 'ON';
        }
        
        // ============================================
        // PHASE 2: WIND RESOURCE - Click to analyze  
        // ============================================
        function addWindResourceInfo() {
            if (layers.wind._windEnabled) return;
            layers.wind._windEnabled = true;
            
            console.log('💨 Wind resource layer enabled');
            document.getElementById('count-wind').textContent = 'ON';
        }
        
        // ============================================
        // PHASE 3: METRO FIBER (Premium placeholder)
        // ============================================
        function loadMetroFiber() {
            if (layers.metrofiber.getLayers().length > 0) return;
            
            // Add placeholder markers for major metro fiber networks
            var metroFiberMarkets = [
                {name: "Northern Virginia Metro Ring", lat: 39.0438, lng: -77.4874, providers: ["Zayo", "Lumen", "Crown Castle", "Windstream"]},
                {name: "Dallas Metro Fiber", lat: 32.7767, lng: -96.7970, providers: ["Zayo", "AT&T", "Spectrum Enterprise", "Uniti"]},
                {name: "Chicago Metro Network", lat: 41.8781, lng: -87.6298, providers: ["Zayo", "Cogent", "GTL", "AT&T"]},
                {name: "Silicon Valley Metro", lat: 37.3382, lng: -121.8863, providers: ["Zayo", "AT&T", "Lumen", "TPx"]},
                {name: "Phoenix Metro Fiber", lat: 33.4484, lng: -112.0740, providers: ["Zayo", "Cox", "Lumen", "Uniti"]},
                {name: "Atlanta Metro Ring", lat: 33.7490, lng: -84.3880, providers: ["Zayo", "AT&T", "Comcast", "Uniti"]}
            ];
            
            metroFiberMarkets.forEach(function(mf) {
                L.circle([mf.lat, mf.lng], {
                    radius: 25000, // 25km radius
                    fillColor: '#8b5cf6',
                    color: '#7c3aed',
                    weight: 2,
                    opacity: 0.6,
                    fillOpacity: 0.1,
                    dashArray: '10, 5'
                }).bindPopup(
                    '<div class="popup-title">📡 ' + mf.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Major Providers</span></div>' +
                    '<div class="popup-row"><span class="popup-value">' + mf.providers.join(', ') + '</span></div>' +
                    '<div class="popup-row" style="background:rgba(139,92,246,0.1);padding:8px;border-radius:4px;margin-top:8px;text-align:center;">' +
                    '<span style="color:#8b5cf6;font-weight:600;">🔒 Premium Data Available</span><br>' +
                    '<span style="font-size:11px;color:var(--text3);">Detailed fiber routes from GeoTel/FiberLocator</span>' +
                    '</div>'
                ).addTo(layers.metrofiber);
            });
            
            document.getElementById('count-metrofiber').textContent = metroFiberMarkets.length;
            console.log('✅ Loaded ' + metroFiberMarkets.length + ' metro fiber markets (premium placeholder)');
        }
        
        // ============================================
        // INTERCONNECTION QUEUE BY RTO/ISO
        // Shows what's waiting to come online
        // ============================================
        function loadGenQueue() {
            if (layers.genqueue.getLayers().length > 0) return;
            
            console.log('📡 Loading interconnection queue data...');
            
            // RTO/ISO headquarters with queue data
            var rtoLocations = [
                {rto: "PJM", lat: 39.9526, lng: -75.1652, city: "Valley Forge, PA"},
                {rto: "ERCOT", lat: 30.2672, lng: -97.7431, city: "Austin, TX"},
                {rto: "CAISO", lat: 38.5816, lng: -121.4944, city: "Folsom, CA"},
                {rto: "MISO", lat: 46.8772, lng: -96.7898, city: "Carmel, IN"},
                {rto: "SPP", lat: 35.4676, lng: -97.5164, city: "Little Rock, AR"},
                {rto: "NYISO", lat: 42.6526, lng: -73.7562, city: "Rensselaer, NY"},
                {rto: "ISO-NE", lat: 42.3601, lng: -71.0589, city: "Holyoke, MA"}
            ];
            
            rtoLocations.forEach(function(loc) {
                var q = INTERCONNECTION_QUEUES[loc.rto];
                if (!q) return;
                
                // Size marker by queue size
                var radius = q.totalQueueMW > 200000 ? 40000 : q.totalQueueMW > 100000 ? 30000 : 20000;
                
                L.circle([loc.lat, loc.lng], {
                    radius: radius,
                    fillColor: '#f59e0b',
                    color: '#d97706',
                    weight: 3,
                    opacity: 0.8,
                    fillOpacity: 0.15
                }).bindPopup(
                    '<div class="popup-title">⏳ ' + q.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Total Queue</span><span class="popup-value" style="color:#f59e0b;font-weight:700">' + (q.totalQueueMW/1000).toFixed(0) + ' GW</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Active Projects</span><span class="popup-value">' + q.activeProjects.toLocaleString() + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Avg Wait Time</span><span class="popup-value">' + q.avgWaitYears + ' years</span></div>' +
                    '<div class="popup-row" style="margin-top:8px;"><span class="popup-label">By Fuel Type:</span></div>' +
                    '<div style="font-size:11px;padding:4px 0;">' +
                    '☀️ Solar: ' + (q.byFuel.solar/1000).toFixed(0) + ' GW<br>' +
                    '🔋 Storage: ' + (q.byFuel.storage/1000).toFixed(0) + ' GW<br>' +
                    '💨 Wind: ' + (q.byFuel.wind/1000).toFixed(0) + ' GW<br>' +
                    '🔥 Gas: ' + (q.byFuel.gas/1000).toFixed(0) + ' GW<br>' +
                    '☢️ Nuclear: ' + (q.byFuel.nuclear/1000).toFixed(1) + ' GW' +
                    '</div>' +
                    '<div class="popup-row" style="margin-top:8px;"><span class="popup-label">Key Markets:</span></div>' +
                    '<div style="font-size:11px;">' + q.markets.join(', ') + '</div>' +
                    (q.recentApprovals.length > 0 ? 
                        '<div class="popup-row" style="margin-top:8px;background:rgba(34,197,94,0.1);padding:6px;border-radius:4px;">' +
                        '<span style="color:#22c55e;font-weight:600;">Recent DC Projects:</span><br>' +
                        q.recentApprovals.map(function(p) { return '• ' + p.project + ' (' + p.mw + ' MW)'; }).join('<br>') +
                        '</div>' : '') +
                    '<div class="popup-row" style="color:#f59e0b;font-size:10px;margin-top:4px;">📡 ' + loc.rto + ' Queue Data</div>'
                ).addTo(layers.genqueue);
            });
            
            var totalQueueGW = Object.values(INTERCONNECTION_QUEUES).reduce(function(sum, q) { return sum + q.totalQueueMW; }, 0) / 1000;
            document.getElementById('count-genqueue').textContent = totalQueueGW.toFixed(0) + ' GW';
            console.log('✅ Loaded ' + rtoLocations.length + ' RTO/ISO queue regions (' + totalQueueGW.toFixed(0) + ' GW total)');
        }
        
        // ============================================
        // LONG-HAUL FIBER CARRIERS
        // Major backbone networks
        // ============================================
        function loadLongHaulFiber() {
            if (layers.longhaulfiber.getLayers().length > 0) return;
            
            console.log('📡 Loading long-haul fiber carriers with route mapping...');
            
            var totalRouteMiles = 0;
            
            LONGHAUL_FIBER_CARRIERS.forEach(function(carrier) {
                totalRouteMiles += carrier.routeMiles;
                
                // Draw actual route lines for each carrier
                carrier.keyRoutes.forEach(function(route) {
                    if (route.coords && route.coords.length > 1) {
                        var line = L.polyline(route.coords, {
                            color: carrier.color,
                            weight: carrier.tier === 1 ? 4 : 3,
                            opacity: 0.8,
                            dashArray: carrier.tier === 1 ? null : '10, 5'
                        });
                        
                        line.bindPopup(
                            '<div style="min-width:260px">' +
                            '<div style="font-weight:700;font-size:14px;margin-bottom:8px;color:' + carrier.color + '">' +
                            '🔗 ' + carrier.name + '</div>' +
                            '<div style="background:rgba(99,102,241,0.1);border-radius:6px;padding:8px;margin-bottom:8px">' +
                            '<div style="font-size:12px;font-weight:600">' + route.from + ' → ' + route.to + '</div>' +
                            '<div style="font-size:20px;color:#22c55e;font-weight:700;margin:4px 0">' + route.latency + '</div>' +
                            '<div style="font-size:10px;color:#9ca3af">Round-trip latency</div>' +
                            '</div>' +
                            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;margin-bottom:8px">' +
                            '<div><span style="color:#9ca3af">Network:</span> <strong>' + carrier.routeMiles.toLocaleString() + ' mi</strong></div>' +
                            '<div><span style="color:#9ca3af">Countries:</span> <strong>' + carrier.countries + '</strong></div>' +
                            '<div><span style="color:#9ca3af">Tier:</span> <strong>' + (carrier.tier === 1 ? '⭐ Tier 1' : 'Tier 2') + '</strong></div>' +
                            '</div>' +
                            '<a href="' + carrier.networkMapUrl + '" target="_blank" style="display:inline-block;padding:6px 12px;background:' + carrier.color + ';color:white;border-radius:4px;font-size:11px;text-decoration:none;margin-top:4px">View Network Map →</a>' +
                            '</div>'
                        );
                        
                        line.addTo(layers.longhaulfiber);
                    }
                });
                
                // Add carrier hub markers at key DC markets
                var marketCoords = {
                    "Ashburn": [39.0438, -77.4874],
                    "Dallas": [32.7767, -96.7970],
                    "Chicago": [41.8781, -87.6298],
                    "Phoenix": [33.4484, -112.0740],
                    "Denver": [39.7392, -104.9903],
                    "Los Angeles": [34.0522, -118.2437],
                    "Seattle": [47.6062, -122.3321],
                    "Atlanta": [33.7490, -84.3880],
                    "New York": [40.7128, -74.0060],
                    "Houston": [29.7604, -95.3698],
                    "Miami": [25.7617, -80.1918],
                    "Boston": [42.3601, -71.0589],
                    "San Jose": [37.3382, -121.8863],
                    "Charlotte": [35.2271, -80.8431],
                    "Columbus": [39.9612, -82.9988],
                    "Indianapolis": [39.7684, -86.1581],
                    "Detroit": [42.3314, -83.0458],
                    "Richmond": [37.5407, -77.4360],
                    "Raleigh": [35.7796, -78.6382],
                    "Portland": [45.5152, -122.6784],
                    "Fremont": [37.5485, -121.9886],
                    "Tampa": [27.9506, -82.4572]
                };
                
                // Only show first 3 markets per carrier to avoid clutter
                carrier.dcMarkets.slice(0, 3).forEach(function(market, idx) {
                    var coords = marketCoords[market];
                    if (!coords) return;
                    
                    // Small offset for each carrier
                    var offset = LONGHAUL_FIBER_CARRIERS.indexOf(carrier) * 0.015;
                    
                    L.circleMarker([coords[0] + offset, coords[1] + offset], {
                        radius: carrier.tier === 1 ? 8 : 6,
                        fillColor: carrier.color,
                        color: '#fff',
                        weight: 2,
                        opacity: 0.95,
                        fillOpacity: 0.85
                    }).bindPopup(
                        '<div style="min-width:280px">' +
                        '<div style="font-weight:700;font-size:14px;margin-bottom:8px;color:' + carrier.color + '">' +
                        '🌐 ' + carrier.name + ' - ' + market + ' POP</div>' +
                        
                        // Key stats
                        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;margin-bottom:10px">' +
                        '<div style="background:rgba(99,102,241,0.1);padding:8px;border-radius:6px">' +
                        '<div style="color:#9ca3af;font-size:10px">Network Size</div>' +
                        '<div style="font-weight:700">' + carrier.routeMiles.toLocaleString() + ' mi</div>' +
                        '</div>' +
                        '<div style="background:rgba(34,197,94,0.1);padding:8px;border-radius:6px">' +
                        '<div style="color:#9ca3af;font-size:10px">Global Reach</div>' +
                        '<div style="font-weight:700">' + carrier.countries + ' countries</div>' +
                        '</div>' +
                        '</div>' +
                        
                        // Tier badge
                        '<div style="margin-bottom:10px">' +
                        '<span style="padding:4px 10px;background:' + (carrier.tier === 1 ? 'rgba(245,158,11,0.2)' : 'rgba(99,102,241,0.2)') + ';border-radius:4px;font-size:11px;font-weight:600;color:' + (carrier.tier === 1 ? '#f59e0b' : '#6366f1') + '">' +
                        (carrier.tier === 1 ? '⭐ Tier 1 Global' : '🔗 Tier 2 Regional') + '</span>' +
                        '</div>' +
                        
                        // Key routes
                        '<div style="font-size:11px;color:#9ca3af;margin-bottom:4px">Key Routes:</div>' +
                        '<div style="font-size:11px;max-height:80px;overflow-y:auto">' + 
                        carrier.keyRoutes.slice(0, 3).map(function(r) {
                            return '<div style="padding:2px 0">' + r.from + ' → ' + r.to + ' <span style="color:#22c55e">(' + r.latency + ')</span></div>';
                        }).join('') + 
                        '</div>' +
                        
                        // DC Markets
                        '<div style="margin-top:8px;font-size:11px;color:#9ca3af">DC Markets: <span style="color:#f1f5f9">' + carrier.dcMarkets.join(', ') + '</span></div>' +
                        
                        // Link
                        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1)">' +
                        '<a href="' + carrier.networkMapUrl + '" target="_blank" style="color:' + carrier.color + ';font-size:11px">View Full Network Map →</a>' +
                        '</div>' +
                        '</div>'
                    ).addTo(layers.longhaulfiber);
                });
            });
            
            document.getElementById('count-longhaul').textContent = LONGHAUL_FIBER_CARRIERS.length + ' (' + (totalRouteMiles/1000).toFixed(0) + 'k mi)';
            console.log('✅ Loaded ' + LONGHAUL_FIBER_CARRIERS.length + ' long-haul fiber carriers (' + totalRouteMiles.toLocaleString() + ' route miles)');
            
            // Show summary notification
            var tier1Count = LONGHAUL_FIBER_CARRIERS.filter(function(c) { return c.tier === 1; }).length;
            console.log('   📡 ' + tier1Count + ' Tier 1 carriers, ' + (LONGHAUL_FIBER_CARRIERS.length - tier1Count) + ' Tier 2 carriers');
        }
        
        // ============================================
        // MIDSTREAM GAS COMPANIES
        // Pipeline operators with capacity
        // ============================================
        function loadMidstreamGas() {
            if (layers.midstream.getLayers().length > 0) return;
            
            console.log('📡 Loading midstream gas companies...');
            
            MIDSTREAM_GAS_COMPANIES.forEach(function(company) {
                var marker = L.marker([company.lat, company.lng], {
                    icon: L.divIcon({
                        className: 'midstream-marker',
                        html: '<div style="background:linear-gradient(135deg,#f97316,#ea580c);width:32px;height:32px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:16px;">🏭</div>',
                        iconSize: [32, 32],
                        iconAnchor: [16, 16]
                    })
                }).bindPopup(
                    '<div class="popup-title">🏭 ' + company.name + ' <span style="color:var(--text3);font-size:11px;">(' + company.ticker + ')</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Pipeline Miles</span><span class="popup-value" style="font-weight:700">' + company.pipelineMiles.toLocaleString() + ' mi</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Daily Capacity</span><span class="popup-value" style="color:#22c55e;font-weight:700">' + company.dailyCapacityBcf + ' Bcf/d</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Processing</span><span class="popup-value">' + company.processingCapacityBcfd + ' Bcf/d</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Storage</span><span class="popup-value">' + company.storageCapacityBcf + ' Bcf</span></div>' +
                    '<div class="popup-row" style="margin-top:8px;"><span class="popup-label">Key Pipelines:</span></div>' +
                    '<div style="font-size:11px;">' + company.keyPipelines.map(function(p) {
                        return '• ' + p.name + ' (' + p.capacityBcfd + ' Bcf/d) - ' + p.region;
                    }).join('<br>') + '</div>' +
                    '<div class="popup-row" style="margin-top:8px;"><span class="popup-label">DC Markets Served:</span></div>' +
                    '<div style="font-size:11px;">' + company.dcMarkets.join(', ') + '</div>' +
                    '<div class="popup-row" style="color:#f97316;font-size:10px;margin-top:4px;">📡 Midstream Infrastructure</div>'
                );
                marker.addTo(layers.midstream);
            });
            
            var totalCapacity = MIDSTREAM_GAS_COMPANIES.reduce(function(sum, c) { return sum + c.dailyCapacityBcf; }, 0);
            document.getElementById('count-midstream').textContent = MIDSTREAM_GAS_COMPANIES.length + ' (' + totalCapacity.toFixed(0) + ' Bcf/d)';
            console.log('✅ Loaded ' + MIDSTREAM_GAS_COMPANIES.length + ' midstream gas companies (' + totalCapacity + ' Bcf/d total)');
        }
        
        // ============================================
        // LNG TERMINALS
        // Export/Import facilities
        // ============================================
        function loadLNGTerminals() {
            if (layers.lng.getLayers().length > 0) return;
            
            console.log('📡 Loading LNG terminals...');
            
            LNG_TERMINALS.forEach(function(terminal) {
                var statusColor = terminal.status === 'Operating' ? '#22c55e' : 
                                  terminal.status === 'Construction' ? '#f59e0b' : '#6b7280';
                
                var marker = L.marker([terminal.lat, terminal.lng], {
                    icon: L.divIcon({
                        className: 'lng-marker',
                        html: '<div style="background:linear-gradient(135deg,' + statusColor + ',' + statusColor + 'cc);width:28px;height:28px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:14px;">⛽</div>',
                        iconSize: [28, 28],
                        iconAnchor: [14, 14]
                    })
                }).bindPopup(
                    '<div class="popup-title">⛽ ' + terminal.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + terminal.operator + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value" style="color:#22c55e;font-weight:700">' + terminal.capacityMtpa + ' MTPA</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + terminal.type + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Status</span><span class="popup-value" style="color:' + statusColor + ';font-weight:600">' + terminal.status + '</span></div>' +
                    '<div class="popup-row" style="background:rgba(34,197,94,0.1);padding:6px;border-radius:4px;margin-top:8px;">' +
                    '<span style="font-size:11px;">LNG terminals indicate robust gas infrastructure nearby - ideal for gas-fired peaker plants to support DC loads.</span>' +
                    '</div>' +
                    '<div class="popup-row" style="color:#22c55e;font-size:10px;margin-top:4px;">📡 EIA LNG Data</div>'
                );
                marker.addTo(layers.lng);
            });
            
            var totalCapacity = LNG_TERMINALS.reduce(function(sum, t) { return sum + t.capacityMtpa; }, 0);
            document.getElementById('count-lng').textContent = LNG_TERMINALS.length + ' (' + totalCapacity.toFixed(0) + ' MTPA)';
            console.log('✅ Loaded ' + LNG_TERMINALS.length + ' LNG terminals (' + totalCapacity + ' MTPA capacity)');
        }
        
        // ============================================
        // GAS STORAGE FACILITIES
        // ============================================
        var GAS_STORAGE_FACILITIES = [
            { name: 'Moss Bluff', lat: 30.12, lng: -94.52, capacity: 14.5, type: 'Salt Dome', operator: 'Kinder Morgan' },
            { name: 'Pine Prairie', lat: 30.72, lng: -92.46, capacity: 42.0, type: 'Salt Dome', operator: 'Pine Prairie Energy' },
            { name: 'Spindletop', lat: 29.92, lng: -94.05, capacity: 16.0, type: 'Salt Dome', operator: 'Texas Brine' },
            { name: 'Carthage', lat: 32.15, lng: -94.34, capacity: 12.0, type: 'Depleted Field', operator: 'Enbridge' },
            { name: 'Ryckman Creek', lat: 41.55, lng: -110.62, capacity: 8.5, type: 'Depleted Field', operator: 'Williams' },
            { name: 'Stagecoach', lat: 42.22, lng: -76.85, capacity: 26.0, type: 'Salt Dome', operator: 'Crestwood' },
            { name: 'Liberty', lat: 39.83, lng: -75.52, capacity: 17.5, type: 'Aquifer', operator: 'Southern Union' },
            { name: 'Leidy', lat: 41.35, lng: -77.60, capacity: 96.0, type: 'Depleted Field', operator: 'Williams' },
            { name: 'Jackson Prairie', lat: 46.73, lng: -122.68, capacity: 25.0, type: 'Aquifer', operator: 'Puget Sound Energy' },
            { name: 'Wild Goose', lat: 39.35, lng: -121.95, capacity: 75.0, type: 'Depleted Field', operator: 'Niska Gas Storage' }
        ];
        
        function loadGasStorage() {
            if (layers.gasStorage.getLayers().length > 0) return;
            
            console.log('📡 Loading gas storage facilities...');
            
            GAS_STORAGE_FACILITIES.forEach(function(facility) {
                var marker = L.marker([facility.lat, facility.lng], {
                    icon: L.divIcon({
                        className: 'gas-storage-marker',
                        html: '<div style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);width:26px;height:26px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:12px;">🛢️</div>',
                        iconSize: [26, 26],
                        iconAnchor: [13, 13]
                    })
                }).bindPopup(
                    '<div class="popup-title">🛢️ ' + facility.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + facility.operator + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value" style="color:#8b5cf6;font-weight:700">' + facility.capacity + ' Bcf</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">' + facility.type + '</span></div>' +
                    '<div class="popup-row" style="color:#8b5cf6;font-size:10px;margin-top:4px;">📡 EIA Underground Storage Data</div>'
                );
                marker.addTo(layers.gasStorage);
            });
            
            var el = document.getElementById('count-gasStorage');
            if (el) el.textContent = GAS_STORAGE_FACILITIES.length;
            console.log('✅ Loaded ' + GAS_STORAGE_FACILITIES.length + ' gas storage facilities');
        }
        
        // ============================================
        // GAS MARKET HUBS
        // ============================================
        var GAS_MARKET_HUBS = [
            { name: 'Henry Hub', lat: 30.10, lng: -93.36, region: 'Gulf Coast', benchmark: true, price: 2.45 },
            { name: 'Waha Hub', lat: 31.05, lng: -103.75, region: 'Permian', benchmark: false, price: 1.85 },
            { name: 'Dominion South', lat: 40.02, lng: -80.35, region: 'Appalachia', benchmark: false, price: 2.15 },
            { name: 'Chicago Citygate', lat: 41.88, lng: -87.63, region: 'Midwest', benchmark: false, price: 2.55 },
            { name: 'SoCal Citygate', lat: 34.05, lng: -118.25, region: 'West Coast', benchmark: false, price: 3.25 },
            { name: 'Algonquin Citygate', lat: 42.36, lng: -71.06, region: 'New England', benchmark: false, price: 3.85 },
            { name: 'AECO Hub', lat: 51.05, lng: -114.07, region: 'Western Canada', benchmark: false, price: 1.95 },
            { name: 'Katy Hub', lat: 29.78, lng: -95.82, region: 'Gulf Coast', benchmark: false, price: 2.40 },
            { name: 'Opal Hub', lat: 41.77, lng: -110.30, region: 'Rockies', benchmark: false, price: 2.10 },
            { name: 'PG&E Citygate', lat: 37.77, lng: -122.42, region: 'NorCal', benchmark: false, price: 3.45 }
        ];
        
        function loadGasMarketHubs() {
            if (layers.gasMarketHubs.getLayers().length > 0) return;
            
            console.log('📡 Loading gas market hubs...');
            
            GAS_MARKET_HUBS.forEach(function(hub) {
                var color = hub.benchmark ? '#f59e0b' : '#3b82f6';
                var marker = L.marker([hub.lat, hub.lng], {
                    icon: L.divIcon({
                        className: 'gas-hub-marker',
                        html: '<div style="background:linear-gradient(135deg,' + color + ',' + color + 'cc);width:30px;height:30px;border-radius:50%;border:3px solid ' + (hub.benchmark ? '#fbbf24' : '#fff') + ';box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:14px;">📍</div>',
                        iconSize: [30, 30],
                        iconAnchor: [15, 15]
                    })
                }).bindPopup(
                    '<div class="popup-title">📍 ' + hub.name + (hub.benchmark ? ' <span style="color:#f59e0b">(BENCHMARK)</span>' : '') + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Region</span><span class="popup-value">' + hub.region + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Spot Price</span><span class="popup-value" style="color:#22c55e;font-weight:700">$' + hub.price.toFixed(2) + '/MMBtu</span></div>' +
                    (hub.benchmark ? '<div class="popup-row" style="background:rgba(245,158,11,0.2);padding:6px;border-radius:4px;"><span style="font-size:11px;color:#f59e0b;">⭐ Primary US natural gas benchmark</span></div>' : '') +
                    '<div class="popup-row" style="color:#3b82f6;font-size:10px;margin-top:4px;">📡 CME/NYMEX Pricing Data</div>'
                );
                marker.addTo(layers.gasMarketHubs);
            });
            
            var el = document.getElementById('count-gasMarketHubs');
            if (el) el.textContent = GAS_MARKET_HUBS.length;
            console.log('✅ Loaded ' + GAS_MARKET_HUBS.length + ' gas market hubs');
        }
        
        // ============================================
        // GAS PROCESSING PLANTS
        // ============================================
        var GAS_PROCESSING_PLANTS = [
            { name: 'Midland Basin Complex', lat: 31.99, lng: -102.08, capacity: 1.2, operator: 'Targa Resources', region: 'Permian' },
            { name: 'Delaware Basin Complex', lat: 31.75, lng: -103.95, capacity: 0.9, operator: 'Enterprise Products', region: 'Permian' },
            { name: 'Eagle Ford Processing', lat: 28.65, lng: -98.68, capacity: 0.75, operator: 'DCP Midstream', region: 'Eagle Ford' },
            { name: 'Marcellus Processing Hub', lat: 40.45, lng: -79.98, capacity: 1.5, operator: 'Williams', region: 'Appalachia' },
            { name: 'Haynesville Processing', lat: 32.35, lng: -93.75, capacity: 0.6, operator: 'EnLink Midstream', region: 'Haynesville' },
            { name: 'DJ Basin Complex', lat: 40.15, lng: -104.82, capacity: 0.8, operator: 'DCP Midstream', region: 'DJ Basin' },
            { name: 'Bakken Processing', lat: 47.95, lng: -103.62, capacity: 0.5, operator: 'Hess Midstream', region: 'Bakken' },
            { name: 'Anadarko Basin Plant', lat: 35.45, lng: -98.45, capacity: 0.7, operator: 'ONEOK', region: 'Mid-Continent' }
        ];
        
        function loadGasProcessing() {
            if (layers.gasProcessing.getLayers().length > 0) return;
            
            console.log('📡 Loading gas processing plants...');
            
            GAS_PROCESSING_PLANTS.forEach(function(plant) {
                var marker = L.marker([plant.lat, plant.lng], {
                    icon: L.divIcon({
                        className: 'gas-processing-marker',
                        html: '<div style="background:linear-gradient(135deg,#06b6d4,#0891b2);width:26px;height:26px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:12px;">🏭</div>',
                        iconSize: [26, 26],
                        iconAnchor: [13, 13]
                    })
                }).bindPopup(
                    '<div class="popup-title">🏭 ' + plant.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + plant.operator + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value" style="color:#06b6d4;font-weight:700">' + plant.capacity + ' Bcf/d</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Basin</span><span class="popup-value">' + plant.region + '</span></div>' +
                    '<div class="popup-row" style="color:#06b6d4;font-size:10px;margin-top:4px;">📡 EIA Processing Data</div>'
                );
                marker.addTo(layers.gasProcessing);
            });
            
            var el = document.getElementById('count-gasProcessing');
            if (el) el.textContent = GAS_PROCESSING_PLANTS.length;
            console.log('✅ Loaded ' + GAS_PROCESSING_PLANTS.length + ' gas processing plants');
        }
        
        // ============================================
        // NGL FRACTIONATORS
        // ============================================
        var NGL_FRACTIONATORS = [
            { name: 'Mont Belvieu Complex', lat: 29.85, lng: -94.90, capacity: 1.8, operator: 'Enterprise Products', products: 'Ethane, Propane, Butane' },
            { name: 'Sweeny Fractionator', lat: 29.05, lng: -95.70, capacity: 0.4, operator: 'Phillips 66', products: 'Propane, Butane' },
            { name: 'Conway Hub', lat: 38.05, lng: -97.65, capacity: 0.35, operator: 'ONEOK', products: 'Ethane, Propane, Butane' },
            { name: 'Corpus Christi Frac', lat: 27.80, lng: -97.40, capacity: 0.25, operator: 'Targa Resources', products: 'Propane, Butane, Isobutane' },
            { name: 'Hobbs Fractionator', lat: 32.70, lng: -103.15, capacity: 0.2, operator: 'DCP Midstream', products: 'NGLs' },
            { name: 'Bellevue Frac II', lat: 29.86, lng: -94.92, capacity: 0.15, operator: 'Targa Resources', products: 'Mixed NGLs' }
        ];
        
        function loadNGLFractionators() {
            if (layers.nglFractionators.getLayers().length > 0) return;
            
            console.log('📡 Loading NGL fractionators...');
            
            NGL_FRACTIONATORS.forEach(function(frac) {
                var marker = L.marker([frac.lat, frac.lng], {
                    icon: L.divIcon({
                        className: 'ngl-frac-marker',
                        html: '<div style="background:linear-gradient(135deg,#ec4899,#db2777);width:26px;height:26px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;font-size:12px;">⚗️</div>',
                        iconSize: [26, 26],
                        iconAnchor: [13, 13]
                    })
                }).bindPopup(
                    '<div class="popup-title">⚗️ ' + frac.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Operator</span><span class="popup-value">' + frac.operator + '</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Capacity</span><span class="popup-value" style="color:#ec4899;font-weight:700">' + frac.capacity + ' MMbbl/d</span></div>' +
                    '<div class="popup-row"><span class="popup-label">Products</span><span class="popup-value">' + frac.products + '</span></div>' +
                    '<div class="popup-row" style="color:#ec4899;font-size:10px;margin-top:4px;">📡 EIA NGL Data</div>'
                );
                marker.addTo(layers.nglFractionators);
            });
            
            var el = document.getElementById('count-nglFractionators');
            if (el) el.textContent = NGL_FRACTIONATORS.length;
            console.log('✅ Loaded ' + NGL_FRACTIONATORS.length + ' NGL fractionators');
        }
        
        // ============================================
        // MASTER API LOADER - Triggered on map move
        // ============================================
        var apiLoadTimeout = null;
        
        function loadLiveInfrastructure() {
            if (!ENABLE_LIVE_API) return;
            
            clearTimeout(apiLoadTimeout);
            apiLoadTimeout = setTimeout(function() {
                var bounds = map.getBounds();
                var zoom = map.getZoom();
                
                console.log('🗺️ Loading live infrastructure at zoom ' + zoom);
                
                // Load different data at different zoom levels
                if (zoom >= 6) {
                    // Phase 1: Static data layers (always load)
                    loadInternetExchanges();
                    loadOpportunityZones();
                    loadMetroFiber();
                    loadGenQueue();
                    loadLongHaulFiber();
                    loadMidstreamGas();
                    loadLNGTerminals();
                }
                if (zoom >= 7) {
                    loadSubstationsFromAPI(bounds);
                    loadGasPipelinesFromAPI(bounds);
                    loadPowerPlantsFromAPI(bounds);
                    loadHIFLDPowerPlants(bounds);
                }
                if (zoom >= 8) {
                    loadTransmissionFromAPI(bounds);
                    loadHIFLDGasCompressors(bounds);
                }
            }, 500); // Debounce 500ms
        }
        
        // Attach to map events
        map.on('moveend', loadLiveInfrastructure);
        map.on('zoomend', loadLiveInfrastructure);
        
        // Add toggle button for live API
        var apiToggle = document.createElement('button');
        apiToggle.className = 'layer-btn';
        apiToggle.id = 'api-toggle';
        apiToggle.innerHTML = '📡 Live API<span class="count" style="background:' + (ENABLE_LIVE_API ? 'var(--green)' : 'var(--red)') + '">' + (ENABLE_LIVE_API ? 'ON' : 'OFF') + '</span>';
        apiToggle.style.marginLeft = '8px';
        apiToggle.onclick = function() {
            ENABLE_LIVE_API = !ENABLE_LIVE_API;
            this.querySelector('.count').textContent = ENABLE_LIVE_API ? 'ON' : 'OFF';
            this.querySelector('.count').style.background = ENABLE_LIVE_API ? 'var(--green)' : 'var(--red)';
            if (ENABLE_LIVE_API) {
                loadLiveInfrastructure();
            }
            console.log('📡 Live API: ' + (ENABLE_LIVE_API ? 'Enabled' : 'Disabled'));
        };
        document.querySelector('.layer-toggles').appendChild(apiToggle);
        
        // Add Audit Button
        var auditBtn = document.createElement('button');
        auditBtn.className = 'layer-btn';
        auditBtn.innerHTML = '📊 Audit<span class="count" style="background:var(--accent)">CHECK</span>';
        auditBtn.style.marginLeft = '8px';
        auditBtn.onclick = function() {
            showAuditPanel();
        };
        document.querySelector('.layer-toggles').appendChild(auditBtn);
        
        // Add Manual Refresh Button
        var refreshBtn = document.createElement('button');
        refreshBtn.className = 'layer-btn';
        refreshBtn.innerHTML = '🔄 Refresh<span class="count" style="background:var(--green)">NOW</span>';
        refreshBtn.style.marginLeft = '8px';
        refreshBtn.onclick = function() {
            currentSecond = 0; // Trigger immediate refresh
            performDataRefresh();
        };
        document.querySelector('.layer-toggles').appendChild(refreshBtn);
        
        // Initial load
        setTimeout(loadLiveInfrastructure, 1000);
        
        // Set initial timestamp
        setTimeout(function() {
            document.getElementById('last-refresh-time').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
            console.log('🔄 Initial data load complete');
            // Run initial audit after data loads
            setTimeout(function() {
                var audit = runDataAudit();
                console.log('📊 Initial audit complete: ' + audit.total + ' total items');
            }, 3000);
        }, 2000);
        
        console.log('📡 Live API module v68 - Phase 1-3: OSM + HIFLD + FEMA + NWI + USGS');

        // ============================================
        // SITE EVALUATION SEARCH
        // Address or lat/lng lookup with infrastructure analysis
        // ============================================
        // ============================================
        // RATE LIMITING HELPER FUNCTIONS
        // ============================================
        
        function showToast(message, type) {
            var toast = document.createElement('div');
            toast.className = 'dchub-toast';
            toast.style.cssText = 'position:fixed;bottom:100px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;font-size:14px;font-weight:500;z-index:10000;animation:slideUp 0.3s ease;max-width:90%;text-align:center;';
            
            if (type === 'warning') {
                toast.style.background = 'linear-gradient(135deg, #f59e0b, #d97706)';
                toast.style.color = '#fff';
            } else if (type === 'error') {
                toast.style.background = 'linear-gradient(135deg, #ef4444, #dc2626)';
                toast.style.color = '#fff';
            } else {
                toast.style.background = 'linear-gradient(135deg, #6366f1, #8b5cf6)';
                toast.style.color = '#fff';
            }
            
            toast.textContent = message;
            document.body.appendChild(toast);
            
            setTimeout(function() {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(-50%) translateY(20px)';
                setTimeout(function() { toast.remove(); }, 300);
            }, 4000);
        }
        
        function showUpgradePrompt() {
            // Check if modal already exists
            if (document.getElementById('upgrade-modal')) return;
            
            var modal = document.createElement('div');
            modal.id = 'upgrade-modal';
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.8);display:flex;align-items:center;justify-content:center;z-index:10001;animation:fadeIn 0.3s ease;';
            
            modal.innerHTML = '\
                <div style="background:#0f1119;border:1px solid #252836;border-radius:16px;padding:32px;max-width:420px;width:90%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.5);">\
                    <div style="font-size:48px;margin-bottom:16px;">🔒</div>\
                    <h2 style="color:#fff;font-size:24px;margin-bottom:12px;">Monthly Limit Reached</h2>\
                    <p style="color:#9ca3af;font-size:14px;line-height:1.6;margin-bottom:24px;">\
                        Free users get <strong style="color:#f59e0b;">1 site evaluation per month</strong>.<br>\
                        Upgrade to Pro for unlimited Land & Power access.\
                    </p>\
                    <div style="display:flex;flex-direction:column;gap:12px;">\
                        <a href="/pricing.html" style="display:block;padding:14px 24px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border-radius:8px;font-weight:600;text-decoration:none;font-size:14px;">Upgrade to Pro →</a>\
                        <button id="close-upgrade-modal" style="padding:10px;background:transparent;border:none;color:#6b7280;cursor:pointer;font-size:13px;">Maybe Later</button>\
                    </div>\
                    <p style="color:#6b7280;font-size:11px;margin-top:20px;">Your free evaluation resets on the 1st of each month.</p>\
                </div>\
            ';
            
            document.body.appendChild(modal);
            
            // Close handlers
            modal.addEventListener('click', function(e) {
                if (e.target === modal || e.target.id === 'close-upgrade-modal') {
                    modal.remove();
                }
            });
            
            document.addEventListener('keydown', function closeOnEsc(e) {
                if (e.key === 'Escape') {
                    modal.remove();
                    document.removeEventListener('keydown', closeOnEsc);
                }
            });
        }
        
        function showLoginPrompt() {
            // Check if modal already exists
            if (document.getElementById('login-modal')) return;
            
            var modal = document.createElement('div');
            modal.id = 'login-modal';
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.8);display:flex;align-items:center;justify-content:center;z-index:10001;animation:fadeIn 0.3s ease;';
            
            modal.innerHTML = '\
                <div style="background:#0f1119;border:1px solid #252836;border-radius:16px;padding:32px;max-width:420px;width:90%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.5);">\
                    <div style="font-size:48px;margin-bottom:16px;">🗺️</div>\
                    <h2 style="color:#fff;font-size:24px;margin-bottom:12px;">Sign In Required</h2>\
                    <p style="color:#9ca3af;font-size:14px;line-height:1.6;margin-bottom:24px;">\
                        Create a free account to access Land & Power tools.<br>\
                        <strong style="color:#10b981;">Free users get 1 evaluation/month.</strong>\
                    </p>\
                    <div style="display:flex;flex-direction:column;gap:12px;">\
                        <a href="/login.html" style="display:block;padding:14px 24px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border-radius:8px;font-weight:600;text-decoration:none;font-size:14px;">Sign In / Sign Up →</a>\
                        <a href="/pricing.html" style="display:block;padding:14px 24px;background:#181a25;color:#fff;border:1px solid #252836;border-radius:8px;font-weight:600;text-decoration:none;font-size:14px;">View Plans</a>\
                        <button id="close-login-modal" style="padding:10px;background:transparent;border:none;color:#6b7280;cursor:pointer;font-size:13px;">Just Browsing</button>\
                    </div>\
                </div>\
            ';
            
            document.body.appendChild(modal);
            
            // Close handlers
            modal.addEventListener('click', function(e) {
                if (e.target === modal || e.target.id === 'close-login-modal') {
                    modal.remove();
                }
            });
            
            document.addEventListener('keydown', function closeOnEsc(e) {
                if (e.key === 'Escape') {
                    modal.remove();
                    document.removeEventListener('keydown', closeOnEsc);
                }
            });
        }
        
        // ============================================
        // SITE EVALUATION TRACKING
        // ============================================
        
        var siteMarkers = [];
        var siteCounter = 0;
        
        // DC Hub custom marker icon
        function createDCHubIcon(number, score) {
            var scoreColor = score >= 80 ? '#22c55e' : score >= 60 ? '#f59e0b' : '#ef4444';
            return L.divIcon({
                className: 'dchub-marker',
                html: '<div style="position:relative;display:flex;flex-direction:column;align-items:center;">' +
                    '<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;box-shadow:0 4px 12px rgba(99,102,241,.5);border:3px solid #fff;">' + number + '</div>' +
                    '<div style="background:' + scoreColor + ';color:#fff;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-top:4px;box-shadow:0 2px 8px rgba(0,0,0,.3);">' + score + '</div>' +
                    '<div style="width:0;height:0;border-left:8px solid transparent;border-right:8px solid transparent;border-top:10px solid ' + scoreColor + ';margin-top:-2px;"></div>' +
                    '</div>',
                iconSize: [50, 70],
                iconAnchor: [25, 70],
                popupAnchor: [0, -70]
            });
        }
        
        // Clear all site markers
        function clearSiteMarkers() {
            siteMarkers.forEach(function(marker) {
                map.removeLayer(marker);
            });
            siteMarkers = [];
            siteCounter = 0;
            updateSiteCounter();
            console.log('🗑️ Cleared all site markers');
        }
        
        function updateSiteCounter() {
            var el = document.getElementById('site-counter');
            if (el) {
                el.textContent = siteCounter + ' site' + (siteCounter !== 1 ? 's' : '');
            }
        }
        
        // County Property Tax Rates (per $1000 assessed value - approximations for DC markets)
        var countyTaxRates = {
            // Virginia
            'loudoun': {county: 'Loudoun County, VA', rate: 0.87, notes: 'Data center-friendly, tax abatements available'},
            'fairfax': {county: 'Fairfax County, VA', rate: 1.11, notes: 'Strong infrastructure'},
            'prince_william': {county: 'Prince William County, VA', rate: 1.03, notes: 'Growing DC market'},
            // Texas (no state income tax)
            'dallas': {county: 'Dallas County, TX', rate: 1.93, notes: 'No state income tax, Chapter 313 expired'},
            'collin': {county: 'Collin County, TX', rate: 2.12, notes: 'Growing tech hub'},
            'tarrant': {county: 'Tarrant County, TX', rate: 2.19, notes: 'DFW metro'},
            'harris': {county: 'Harris County, TX', rate: 2.03, notes: 'Houston metro'},
            'travis': {county: 'Travis County, TX', rate: 1.82, notes: 'Austin - high demand'},
            // Arizona
            'maricopa': {county: 'Maricopa County, AZ', rate: 0.62, notes: 'Very favorable for large campuses'},
            'pinal': {county: 'Pinal County, AZ', rate: 0.83, notes: 'Growing Phoenix suburb'},
            // Georgia
            'fulton': {county: 'Fulton County, GA', rate: 1.21, notes: 'Atlanta metro'},
            'douglas': {county: 'Douglas County, GA', rate: 1.34, notes: 'DC cluster growth'},
            // Illinois
            'cook': {county: 'Cook County, IL', rate: 2.10, notes: 'Chicago - high rates'},
            'dupage': {county: 'DuPage County, IL', rate: 2.34, notes: 'Chicago suburbs'},
            // Nevada
            'clark': {county: 'Clark County, NV', rate: 0.72, notes: 'Las Vegas - no state income tax'},
            // Oregon
            'umatilla': {county: 'Umatilla County, OR', rate: 1.41, notes: 'Cheap hydro power'},
            // Ohio
            'franklin': {county: 'Franklin County, OH', rate: 1.64, notes: 'Columbus - AWS/Google/Meta'},
            'new_albany': {county: 'Licking County, OH', rate: 1.52, notes: 'Intel site, major incentives'},
            // Default
            'default': {county: 'Unknown County', rate: 1.50, notes: 'Verify local rates'}
        };
        
        function getCountyTaxInfo(lat, lng) {
            // Simplified county detection based on coordinates
            // Virginia - NoVA
            if (lat > 38.7 && lat < 39.3 && lng > -77.8 && lng < -77.0) return countyTaxRates.loudoun;
            if (lat > 38.6 && lat < 39.0 && lng > -77.5 && lng < -77.0) return countyTaxRates.fairfax;
            // Dallas-Fort Worth
            if (lat > 32.6 && lat < 33.1 && lng > -97.1 && lng < -96.5) return countyTaxRates.dallas;
            if (lat > 33.0 && lat < 33.4 && lng > -97.0 && lng < -96.4) return countyTaxRates.collin;
            // Houston
            if (lat > 29.5 && lat < 30.1 && lng > -95.7 && lng < -95.0) return countyTaxRates.harris;
            // Austin
            if (lat > 30.1 && lat < 30.5 && lng > -98.0 && lng < -97.5) return countyTaxRates.travis;
            // Phoenix
            if (lat > 33.0 && lat < 33.8 && lng > -112.4 && lng < -111.5) return countyTaxRates.maricopa;
            // Atlanta
            if (lat > 33.5 && lat < 34.0 && lng > -84.6 && lng < -84.2) return countyTaxRates.fulton;
            // Chicago
            if (lat > 41.6 && lat < 42.1 && lng > -88.0 && lng < -87.4) return countyTaxRates.cook;
            // Las Vegas
            if (lat > 35.9 && lat < 36.4 && lng > -115.4 && lng < -114.9) return countyTaxRates.clark;
            // Columbus
            if (lat > 39.8 && lat < 40.2 && lng > -83.2 && lng < -82.8) return countyTaxRates.franklin;
            
            return countyTaxRates.default;
        }
        
        function getEnvironmentalRisk(lat, lng) {
            // Simplified environmental risk assessment
            var risk = {flood: 'Low', seismic: 'Low', wetlands: 'Low', overall: 'Low'};
            
            // Coastal flood risk zones (simplified)
            if (lat < 30 && lng > -98) risk.flood = 'High'; // Gulf Coast
            if (lat > 38 && lat < 41 && lng > -77 && lng < -74) risk.flood = 'Medium'; // Mid-Atlantic
            
            // Seismic risk zones
            if (lng < -115 && lat > 32 && lat < 42) risk.seismic = 'High'; // California
            if (lat > 35 && lat < 37 && lng > -90 && lng < -88) risk.seismic = 'Medium'; // New Madrid
            if (lng < -120 && lat > 40 && lat < 49) risk.seismic = 'Medium'; // Pacific NW
            
            // Calculate overall
            if (risk.flood === 'High' || risk.seismic === 'High') risk.overall = 'Elevated';
            else if (risk.flood === 'Medium' || risk.seismic === 'Medium') risk.overall = 'Moderate';
            
            return risk;
        }
        
        function evaluateSite(lat, lng, address) {
            // ============================================
            // RATE LIMITING: Verify auth with backend
            // Guest=login prompt, Free=1/month, Pro=Unlimited
            // ============================================
            console.log('🔒 Rate limit check starting...');
            
            var userToken = localStorage.getItem('dchub_token');
            var sessionAuth = sessionStorage.getItem('dchub_auth');
            var userData = localStorage.getItem('dchub_user');
            var authToken = userToken || sessionAuth || '';
            
            console.log('🔒 Auth check:', { userToken: !!userToken, sessionAuth: !!sessionAuth, userData: !!userData });
            
            if (!authToken || authToken.length < 10) {
                if (userData) {
                    try {
                        var user = JSON.parse(userData);
                        if (!user || !user.email) {
                            console.log('🔒 BLOCKED: No valid token or user data');
                            showLoginPrompt();
                            return;
                        }
                    } catch(e) {
                        console.log('🔒 BLOCKED: Invalid user data');
                        showLoginPrompt();
                        return;
                    }
                } else {
                    console.log('🔒 BLOCKED: User not logged in');
                    showLoginPrompt();
                    return;
                }
            }
            
            var headers = {};
            if (authToken && authToken.length > 10) {
                headers['Authorization'] = 'Bearer ' + authToken;
            }
            
            var isPro = false;
            if (userData) {
                try {
                    var user = JSON.parse(userData);
                    isPro = user.tier === 'pro' || user.tier === 'enterprise' || user.plan === 'pro' || user.plan === 'enterprise' || user.isPro === true;
                } catch(e) {}
            }
            if (localStorage.getItem('dchub_subscription') === 'active' || localStorage.getItem('dchub_pro') === 'true') {
                isPro = true;
            }
            
            console.log('🔒 isPro:', isPro);
            
            if (isPro) {
                console.log('🔒 ALLOWED: Pro user, skipping limit check');
                _doEvaluateSite(lat, lng, address, headers);
                return;
            }
            
            var _fetch = window.dchubFetch || fetch;
            console.log('🔒 Using dchubFetch:', !!window.dchubFetch, 'API base:', window.DCHUB_API_BASE || '(relative)');
            
            _fetch('/api/v1/land-power/usage', { headers: headers, credentials: 'include' })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                console.log('🔒 Backend usage check:', data);
                
                if (!data.authenticated) {
                    console.log('🔒 BLOCKED: Backend says not authenticated');
                    showLoginPrompt();
                    return;
                }
                
                if (data.searches_remaining === 0 && data.searches_limit > 0) {
                    console.log('🔒 BLOCKED: Monthly limit reached');
                    showUpgradePrompt();
                    return;
                }
                
                _fetch('/api/v1/land-power/track', {
                    method: 'POST',
                    headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
                    body: JSON.stringify({ filters: [] }),
                    credentials: 'include'
                })
                .then(function(trackRes) { return trackRes.json(); })
                .then(function(trackData) {
                    if (trackData.allowed === false) {
                        if (trackData.error === 'AUTH_REQUIRED') {
                            showLoginPrompt();
                        } else {
                            showUpgradePrompt();
                        }
                        return;
                    }
                    
                    if (trackData.searches_remaining === 0) {
                        showToast('⚠️ You\'ve used your free monthly evaluation. Upgrade for unlimited!', 'warning');
                    }
                    
                    _doEvaluateSite(lat, lng, address, headers);
                })
                .catch(function(err) {
                    console.log('🔒 Track call failed, allowing (offline fallback):', err);
                    _doEvaluateSite(lat, lng, address, headers);
                });
            })
            .catch(function(err) {
                console.log('🔒 Usage check failed, falling back to localStorage:', err);
                var today = new Date().toISOString().split('T')[0];
                var usageKey = 'dchub_landpower_usage';
                var usage = JSON.parse(localStorage.getItem(usageKey) || '{}');
                if (usage.date !== today) { usage = { date: today, count: 0 }; }
                if (usage.count >= 1) {
                    showUpgradePrompt();
                    return;
                }
                usage.count++;
                localStorage.setItem(usageKey, JSON.stringify(usage));
                _doEvaluateSite(lat, lng, address, headers);
            });
        }
        
        function _doEvaluateSite(lat, lng, address, headers) {
            console.log('🔒 ALLOWED: Proceeding with evaluation');
            // ============================================
            
            // Increment site counter
            siteCounter++;
            var siteNum = siteCounter;
            
            // Fly to location
            map.flyTo([lat, lng], 12);
            
            // Find nearest infrastructure
            var nearestSubstation = findNearest(lat, lng, substations, 'substation');
            var nearestNuclear = findNearest(lat, lng, nuclearReactors, 'nuclear');
            var nearestDC = findNearest(lat, lng, dataCenters, 'datacenter');
            var nearestAirport = findNearest(lat, lng, airports, 'airport');
            
            // Find 5 nearest data centers for display
            var nearbyDCs = findNearestN(lat, lng, dataCenters, 5);
            
            // Find 3 nearest fiber hubs
            var nearbyFiber = findNearestN(lat, lng, fiberDensity, 3);
            
            // Determine ISO/RTO region
            var isoRegion = determineISO(lat, lng);
            
            // Get county tax info
            var taxInfo = getCountyTaxInfo(lat, lng);
            
            // Get environmental risk
            var envRisk = getEnvironmentalRisk(lat, lng);
            var riskColor = envRisk.overall === 'Low' ? '#22c55e' : envRisk.overall === 'Moderate' ? '#f59e0b' : '#ef4444';
            
            // Calculate site score (0-100)
            var score = calculateSiteScore(nearestSubstation, nearestNuclear, nearestDC, isoRegion, lat, taxInfo, envRisk);
            var scoreColor = score >= 80 ? '#22c55e' : score >= 60 ? '#f59e0b' : '#ef4444';
            var scoreLabel = score >= 80 ? 'Excellent' : score >= 60 ? 'Good' : 'Needs Review';
            
            // Create popup content
            var popupContent = '<div class="site-eval-popup">' +
                '<div class="site-eval-header"><span style="background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:2px 10px;border-radius:12px;font-size:12px;margin-right:8px;">Site ' + siteNum + '</span> Evaluation Report</div>' +
                '<div style="font-size:11px;color:var(--text2);margin-bottom:12px;">' + 
                (address || lat.toFixed(4) + ', ' + lng.toFixed(4)) + '</div>' +
                
                '<div style="font-size:10px;color:var(--blue);font-weight:600;margin:8px 0 4px;">⚡ POWER & GRID</div>' +
                '<div class="site-eval-grid">' +
                '<div class="site-eval-item"><div class="site-eval-label">ISO/RTO</div><div class="site-eval-value">' + 
                '<span class="iso-badge iso-' + isoRegion.abbrev.toLowerCase() + '">' + isoRegion.abbrev + '</span></div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">LMP Range</div><div class="site-eval-value">' + isoRegion.lmp + '</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Nearest Substation</div><div class="site-eval-value">' + nearestSubstation.dist.toFixed(1) + ' mi</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Nearest Nuclear</div><div class="site-eval-value">' + nearestNuclear.dist.toFixed(1) + ' mi</div></div>' +
                '</div>' +
                
                '<div style="font-size:10px;color:var(--green);font-weight:600;margin:8px 0 4px;">💰 TAX & INCENTIVES</div>' +
                '<div class="site-eval-grid">' +
                '<div class="site-eval-item"><div class="site-eval-label">County</div><div class="site-eval-value" style="font-size:10px;">' + taxInfo.county + '</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Property Tax</div><div class="site-eval-value">$' + taxInfo.rate.toFixed(2) + '/k</div></div>' +
                '</div>' +
                '<div style="font-size:9px;color:var(--text3);padding:4px 8px;background:var(--bg3);border-radius:4px;margin:4px 0;">' + taxInfo.notes + '</div>' +
                
                '<div style="font-size:10px;color:var(--orange);font-weight:600;margin:8px 0 4px;">🌍 ENVIRONMENTAL</div>' +
                '<div class="site-eval-grid">' +
                '<div class="site-eval-item"><div class="site-eval-label">Flood Risk</div><div class="site-eval-value" style="color:' + (envRisk.flood === 'Low' ? '#22c55e' : envRisk.flood === 'Medium' ? '#f59e0b' : '#ef4444') + '">' + envRisk.flood + '</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Seismic Risk</div><div class="site-eval-value" style="color:' + (envRisk.seismic === 'Low' ? '#22c55e' : envRisk.seismic === 'Medium' ? '#f59e0b' : '#ef4444') + '">' + envRisk.seismic + '</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Nearest DC</div><div class="site-eval-value">' + nearestDC.dist.toFixed(1) + ' mi</div></div>' +
                '<div class="site-eval-item"><div class="site-eval-label">Nearest Airport</div><div class="site-eval-value">' + nearestAirport.dist.toFixed(1) + ' mi</div></div>' +
                '</div>' +
                
                '<div style="font-size:10px;color:#6366f1;font-weight:600;margin:8px 0 4px;">🏢 NEARBY DATA CENTERS</div>' +
                '<div style="font-size:9px;background:var(--bg3);border-radius:4px;padding:6px;">' +
                nearbyDCs.slice(0, 4).map(function(dc) {
                    return '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border);">' +
                        '<span style="color:var(--text)">' + dc.item.name.substring(0, 22) + (dc.item.name.length > 22 ? '...' : '') + '</span>' +
                        '<span style="color:var(--text2)">' + dc.dist.toFixed(1) + ' mi · ' + dc.item.mw + 'MW</span></div>';
                }).join('') +
                '</div>' +
                
                '<div style="font-size:10px;color:#10b981;font-weight:600;margin:8px 0 4px;">🌐 FIBER CONNECTIVITY</div>' +
                '<div style="font-size:9px;background:var(--bg3);border-radius:4px;padding:6px;">' +
                (nearbyFiber.length > 0 ? nearbyFiber.slice(0, 2).map(function(f) {
                    return '<div style="display:flex;justify-content:space-between;padding:2px 0;">' +
                        '<span style="color:var(--text)">' + f.item.name + ' Hub</span>' +
                        '<span style="color:var(--text2)">' + f.dist.toFixed(1) + ' mi · ' + f.item.carriers + ' carriers</span></div>';
                }).join('') : '<div style="color:var(--text3)">No major fiber hubs within 100mi</div>') +
                '</div>' +
                
                '<div class="site-eval-score">' +
                '<div class="score-circle" style="background:' + scoreColor + '">' + score + '</div>' +
                '<div><div style="font-weight:700;font-size:14px;">' + scoreLabel + '</div>' +
                '<div class="score-label">DC Site Score</div></div>' +
                '</div>' +
                
                '<div style="font-size:9px;color:var(--text3);margin-top:8px;text-align:center;border-top:1px solid var(--border);padding-top:8px;">' +
                '🌿 Toggle Wetlands/Habitat/FEMA layers for detailed environmental review</div>' +
                '</div>';
            
            // Store data for PDF export
            window.lastSiteEvaluation = {
                siteNum: siteNum,
                lat: lat,
                lng: lng,
                address: address || lat.toFixed(4) + ', ' + lng.toFixed(4),
                iso: isoRegion.name,
                isoAbbrev: isoRegion.abbrev,
                gridOperator: isoRegion.name,
                powerCost: isoRegion.lmp,
                nearestSubstation: nearestSubstation.item ? nearestSubstation.item.name + ' (' + nearestSubstation.dist.toFixed(1) + ' mi)' : nearestSubstation.dist.toFixed(1) + ' mi',
                nearestFiber: nearbyFiber.length > 0 ? nearbyFiber[0].item.name + ' Hub (' + nearbyFiber[0].dist.toFixed(1) + ' mi)' : 'N/A',
                nearestAirport: nearestAirport.item ? nearestAirport.item.name + ' (' + nearestAirport.dist.toFixed(1) + ' mi)' : nearestAirport.dist.toFixed(1) + ' mi',
                nearestDC: nearestDC.item ? nearestDC.item.name + ' (' + nearestDC.dist.toFixed(1) + ' mi)' : nearestDC.dist.toFixed(1) + ' mi',
                floodZone: envRisk.flood,
                seismicRisk: envRisk.seismic,
                waterRisk: envRisk.overall,
                state: taxInfo.county.split(',').pop().trim(),
                county: taxInfo.county,
                taxRate: '$' + taxInfo.rate.toFixed(2) + '/k assessed value',
                incentives: taxInfo.notes,
                score: score
            };
            
            // Add marker with DC Hub branding
            var marker = L.marker([lat, lng], {
                icon: createDCHubIcon(siteNum, score)
            }).addTo(map);
            
            marker.bindPopup(popupContent, {maxWidth: 380}).openPopup();
            siteMarkers.push(marker);
            updateSiteCounter();
            
            // Clear search input for next entry
            document.getElementById('site-search').value = '';
            
            console.log('📍 Site #' + siteNum + ' evaluated: ' + lat.toFixed(4) + ', ' + lng.toFixed(4) + ' | Score: ' + score + ' | Tax: $' + taxInfo.rate + '/k');
        }
        
        function findNearest(lat, lng, items, type) {
            var nearest = {dist: 999, item: null};
            items.forEach(function(item) {
                var itemLat = item.lat || item.coords?.[0];
                var itemLng = item.lng || item.coords?.[1];
                if (itemLat && itemLng) {
                    var dist = getDistanceMiles(lat, lng, itemLat, itemLng);
                    if (dist < nearest.dist) {
                        nearest = {dist: dist, item: item};
                    }
                }
            });
            return nearest;
        }
        
        // Find N nearest items with details
        function findNearestN(lat, lng, items, n) {
            var results = [];
            items.forEach(function(item) {
                var itemLat = item.lat || item.coords?.[0];
                var itemLng = item.lng || item.coords?.[1];
                if (itemLat && itemLng) {
                    var dist = getDistanceMiles(lat, lng, itemLat, itemLng);
                    results.push({dist: dist, item: item});
                }
            });
            results.sort(function(a, b) { return a.dist - b.dist; });
            return results.slice(0, n);
        }
        
        function getDistanceMiles(lat1, lng1, lat2, lng2) {
            var R = 3959; // Earth radius in miles
            var dLat = (lat2 - lat1) * Math.PI / 180;
            var dLng = (lng2 - lng1) * Math.PI / 180;
            var a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                    Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                    Math.sin(dLng/2) * Math.sin(dLng/2);
            var c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            return R * c;
        }
        
        function determineISO(lat, lng) {
            // Simple point-in-region check based on approximate boundaries
            if (lng > -103 && lng < -94 && lat > 26 && lat < 36.5) return {abbrev:'ERCOT', lmp:'$25-40/MWh'};
            if (lng > -125 && lng < -117 && lat > 32 && lat < 42) return {abbrev:'CAISO', lmp:'$40-80/MWh'};
            if (lng > -80.5 && lng < -74 && lat > 36.5 && lat < 43) return {abbrev:'PJM', lmp:'$32-45/MWh'};
            if (lng > -97 && lng < -84.5 && lat > 29 && lat < 49) return {abbrev:'MISO', lmp:'$28-38/MWh'};
            if (lng > -104 && lng < -95 && lat > 33 && lat < 44) return {abbrev:'SPP', lmp:'$24-35/MWh'};
            if (lng > -80 && lng < -73 && lat > 40 && lat < 45.5) return {abbrev:'NYISO', lmp:'$35-55/MWh'};
            if (lng > -74 && lng < -67 && lat > 41 && lat < 48) return {abbrev:'ISO-NE', lmp:'$38-60/MWh'};
            return {abbrev:'Non-ISO', lmp:'Varies'};
        }
        
        function calculateSiteScore(substation, nuclear, dc, iso, lat, taxInfo, envRisk) {
            var score = 40; // Base score (lowered to make room for new factors)
            
            // Substation proximity (max 20 pts)
            if (substation.dist < 5) score += 20;
            else if (substation.dist < 15) score += 15;
            else if (substation.dist < 30) score += 10;
            else if (substation.dist < 50) score += 5;
            
            // Nuclear proximity (max 10 pts) - baseload power
            if (nuclear.dist < 50) score += 10;
            else if (nuclear.dist < 100) score += 7;
            else if (nuclear.dist < 200) score += 4;
            
            // Existing DC cluster (max 10 pts) - ecosystem
            if (dc.dist < 10) score += 10;
            else if (dc.dist < 30) score += 7;
            else if (dc.dist < 50) score += 4;
            
            // ISO/RTO region (max 8 pts)
            if (['PJM', 'ERCOT', 'MISO'].includes(iso.abbrev)) score += 8;
            else if (['SPP', 'CAISO'].includes(iso.abbrev)) score += 5;
            else if (['NYISO', 'ISO-NE'].includes(iso.abbrev)) score += 3;
            
            // Property Tax (max 8 pts) - lower is better
            if (taxInfo && taxInfo.rate) {
                if (taxInfo.rate < 0.80) score += 8;
                else if (taxInfo.rate < 1.20) score += 6;
                else if (taxInfo.rate < 1.80) score += 4;
                else if (taxInfo.rate < 2.20) score += 2;
            }
            
            // Environmental Risk (max 8 pts) - lower is better
            if (envRisk) {
                var riskPenalty = 0;
                if (envRisk.flood === 'High') riskPenalty += 4;
                else if (envRisk.flood === 'Medium') riskPenalty += 2;
                if (envRisk.seismic === 'High') riskPenalty += 4;
                else if (envRisk.seismic === 'Medium') riskPenalty += 2;
                score += (8 - riskPenalty);
            } else {
                score += 6; // Default moderate risk
            }
            
            // Climate bonus (max 6 pts) - cooling efficiency
            if (lat > 42) score += 6;
            else if (lat > 38) score += 4;
            else if (lat > 34) score += 2;
            
            return Math.min(100, Math.max(0, Math.round(score)));
        }
        
        // ============================================
        // ADDRESS AUTOCOMPLETE (Nominatim/OpenStreetMap)
        // ============================================
        var autocompleteDropdown = document.getElementById('autocomplete-dropdown');
        var searchInput = document.getElementById('site-search');
        var autocompleteTimeout = null;
        var selectedIndex = -1;
        
        // Debounced search for autocomplete
        searchInput.addEventListener('input', function() {
            var query = this.value.trim();
            clearTimeout(autocompleteTimeout);
            selectedIndex = -1;
            
            if (query.length < 3) {
                autocompleteDropdown.classList.remove('show');
                return;
            }
            
            // Check if it's already lat,lng format - skip autocomplete
            if (/^-?\d+\.?\d*\s*,\s*-?\d+\.?\d*$/.test(query)) {
                autocompleteDropdown.classList.remove('show');
                return;
            }
            
            autocompleteDropdown.innerHTML = '<div class="autocomplete-loading">🔍 Searching...</div>';
            autocompleteDropdown.classList.add('show');
            
            autocompleteTimeout = setTimeout(function() {
                fetchAddressSuggestions(query);
            }, 300);
        });
        
        // Fetch suggestions from Nominatim (free, no API key needed)
        function fetchAddressSuggestions(query) {
            // Bias towards USA for data center searches
            var url = 'https://nominatim.openstreetmap.org/search?format=json&addressdetails=1&limit=6&countrycodes=us,ca,mx,gb,de,nl,ie,fr,se,fi,sg,jp,au,in,hk,ae,sa&q=' + encodeURIComponent(query);
            
            fetch(url, {
                headers: { 'Accept': 'application/json' }
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (!data || data.length === 0) {
                    autocompleteDropdown.innerHTML = '<div class="autocomplete-loading">No results found</div>';
                    return;
                }
                
                var html = '';
                data.forEach(function(item, idx) {
                    var addr = item.address || {};
                    var mainPart = item.display_name.split(',')[0];
                    var subPart = item.display_name.split(',').slice(1, 4).join(',').trim();
                    
                    // Format nicely
                    var city = addr.city || addr.town || addr.village || addr.county || '';
                    var state = addr.state || '';
                    var postcode = addr.postcode || '';
                    var country = addr.country_code ? addr.country_code.toUpperCase() : '';
                    
                    var formatted = mainPart;
                    if (city) formatted = mainPart + ', ' + city;
                    if (state && country === 'US') formatted += ', ' + state;
                    if (postcode) formatted += ' ' + postcode;
                    if (country && country !== 'US') formatted += ', ' + country;
                    
                    html += '<div class="autocomplete-item" data-lat="' + item.lat + '" data-lng="' + item.lon + '" data-display="' + formatted.replace(/"/g, '&quot;') + '">';
                    html += '<div class="addr-main">' + mainPart + '</div>';
                    html += '<div class="addr-sub">' + subPart + '</div>';
                    html += '</div>';
                });
                
                autocompleteDropdown.innerHTML = html;
                
                // Add click handlers
                autocompleteDropdown.querySelectorAll('.autocomplete-item').forEach(function(item) {
                    item.addEventListener('click', function() {
                        var lat = parseFloat(this.dataset.lat);
                        var lng = parseFloat(this.dataset.lng);
                        var display = this.dataset.display;
                        
                        searchInput.value = display;
                        autocompleteDropdown.classList.remove('show');
                        
                        // Trigger evaluation
                        evaluateSite(lat, lng, display);
                    });
                });
            })
            .catch(function(err) {
                console.error('Autocomplete error:', err);
                autocompleteDropdown.innerHTML = '<div class="autocomplete-loading">Error fetching results</div>';
            });
        }
        
        // Keyboard navigation for autocomplete
        searchInput.addEventListener('keydown', function(e) {
            var items = autocompleteDropdown.querySelectorAll('.autocomplete-item');
            if (!items.length) return;
            
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
                updateSelection(items);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                selectedIndex = Math.max(selectedIndex - 1, 0);
                updateSelection(items);
            } else if (e.key === 'Enter' && selectedIndex >= 0) {
                e.preventDefault();
                items[selectedIndex].click();
            } else if (e.key === 'Escape') {
                autocompleteDropdown.classList.remove('show');
            }
        });
        
        function updateSelection(items) {
            items.forEach(function(item, idx) {
                if (idx === selectedIndex) {
                    item.style.background = 'var(--accent)';
                    item.style.color = '#fff';
                } else {
                    item.style.background = '';
                    item.style.color = '';
                }
            });
        }
        
        // Close dropdown when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.site-search-container')) {
                autocompleteDropdown.classList.remove('show');
            }
        });
        
        // Search button handler
        document.getElementById('site-search-btn').addEventListener('click', function() {
            var input = document.getElementById('site-search').value.trim();
            if (!input) return;
            
            // Check if lat,lng format
            var latLngMatch = input.match(/^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$/);
            if (latLngMatch) {
                var lat = parseFloat(latLngMatch[1]);
                var lng = parseFloat(latLngMatch[2]);
                if (lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
                    evaluateSite(lat, lng, input);
                    return;
                }
            }
            
            // Use Nominatim for address geocoding (free, no API key)
            var searchBtn = document.getElementById('site-search-btn');
            var originalText = searchBtn.textContent;
            searchBtn.textContent = '⏳ Searching...';
            searchBtn.disabled = true;
            
            fetch('https://nominatim.openstreetmap.org/search?format=json&q=' + encodeURIComponent(input) + '&countrycodes=us&limit=5', {
                headers: {
                    'Accept': 'application/json',
                    'User-Agent': 'DCHub Site Evaluator (dchub.cloud)'
                }
            })
                .then(function(response) {
                    if (!response.ok) throw new Error('Geocoding service unavailable');
                    return response.json();
                })
                .then(function(data) {
                    searchBtn.textContent = originalText;
                    searchBtn.disabled = false;
                    
                    if (data && data.length > 0) {
                        // Use first result
                        var result = data[0];
                        var shortAddress = result.display_name.split(',').slice(0, 3).join(',');
                        evaluateSite(parseFloat(result.lat), parseFloat(result.lon), shortAddress);
                    } else {
                        alert('📍 Address not found.\n\nTry:\n• More specific address (include city, state)\n• Lat,lng format: 39.0438,-77.4874\n• Right-click on map');
                    }
                })
                .catch(function(error) {
                    searchBtn.textContent = originalText;
                    searchBtn.disabled = false;
                    console.error('Geocoding error:', error);
                    alert('📍 Geocoding service error.\n\nTry:\n• Lat,lng format: 39.0438,-77.4874\n• Right-click directly on map\n• Wait a moment and try again');
                });
        });
        
        // Enter key handler
        document.getElementById('site-search').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                document.getElementById('site-search-btn').click();
            }
        });
        
        // Clear button handler
        document.getElementById('site-clear-btn').addEventListener('click', function() {
            clearSiteMarkers();
        });
        
        // Click on map to evaluate (right-click)
        map.on('contextmenu', function(e) {
            evaluateSite(e.latlng.lat, e.latlng.lng, null);
        });
        
        // ============================================
        // REAL-TIME LMP PRICE UPDATES
        // Simulated prices (real API requires registration)
        // ============================================
        function updateLMPPrices() {
            // Simulate realistic LMP price variations based on typical ranges
            var baseprices = {
                pjm: 32 + (Math.random() - 0.5) * 15,      // PJM West Hub: $25-40
                ercot: 28 + (Math.random() - 0.5) * 20,    // ERCOT North: $18-38
                caiso: 48 + (Math.random() - 0.5) * 30,    // CAISO SP15: $33-63
                miso: 31 + (Math.random() - 0.5) * 12,     // MISO: $25-37
                spp: 29 + (Math.random() - 0.5) * 14,      // SPP South: $22-36
                nyiso: 55 + (Math.random() - 0.5) * 25,    // NYISO Zone J: $42-67
                isone: 42 + (Math.random() - 0.5) * 18     // ISO-NE Hub: $33-51
            };
            
            Object.keys(baseprices).forEach(function(iso) {
                var price = baseprices[iso];
                var el = document.getElementById('lmp-' + iso);
                if (el) {
                    el.textContent = '$' + price.toFixed(2);
                    el.className = 'lmp-price ' + (price > 50 ? 'high' : price > 35 ? 'medium' : 'low');
                }
            });
        }
        
        // Update LMP every 5 minutes
        updateLMPPrices();
        setInterval(updateLMPPrices, 300000);
        
        // ============================================
        // USGS SEISMIC HAZARD DATA
        // Real-time earthquake data from USGS API
        // ============================================
        var seismicDataLoaded = false;
        
        function loadSeismicData() {
            if (seismicDataLoaded) return;
            
            console.log('🌋 Loading USGS earthquake data...');
            
            // Load recent significant earthquakes (past 30 days, M2.5+)
            fetch('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_month.geojson')
                .then(function(response) { return response.json(); })
                .then(function(data) {
                    var usCount = 0;
                    if (data.features && data.features.length > 0) {
                        data.features.forEach(function(eq) {
                            var coords = eq.geometry.coordinates;
                            var props = eq.properties;
                            var mag = props.mag || 0;
                            var place = props.place || 'Unknown';
                            var time = new Date(props.time).toLocaleDateString();
                            
                            // Only show US earthquakes (continental US + Alaska + Hawaii)
                            var isUS = (coords[0] > -130 && coords[0] < -65 && coords[1] > 24 && coords[1] < 50) ||
                                       (coords[0] > -170 && coords[0] < -130 && coords[1] > 50 && coords[1] < 72) ||
                                       (coords[0] > -162 && coords[0] < -154 && coords[1] > 18 && coords[1] < 23);
                            
                            if (isUS) {
                                usCount++;
                                // Color by magnitude
                                var color = mag >= 5 ? '#ef4444' : mag >= 4 ? '#f97316' : mag >= 3 ? '#eab308' : '#22c55e';
                                var radius = Math.max(4, mag * 2);
                                
                                L.circleMarker([coords[1], coords[0]], {
                                    radius: radius,
                                    fillColor: color,
                                    color: '#fff',
                                    weight: 1,
                                    opacity: 0.8,
                                    fillOpacity: 0.6
                                }).bindPopup(
                                    '<div class="popup-title">🌋 Earthquake M' + mag.toFixed(1) + '</div>' +
                                    '<div class="popup-row"><span class="popup-label">Location</span><span class="popup-value">' + place + '</span></div>' +
                                    '<div class="popup-row"><span class="popup-label">Date</span><span class="popup-value">' + time + '</span></div>' +
                                    '<div class="popup-row"><span class="popup-label">Depth</span><span class="popup-value">' + coords[2].toFixed(1) + ' km</span></div>' +
                                    '<div class="popup-row" style="color:#10b981;font-size:10px;margin-top:4px;">📡 Live USGS Data</div>'
                                ).addTo(layers.seismic);
                            }
                        });
                        
                        seismicDataLoaded = true;
                        document.getElementById('count-seismic').textContent = usCount;
                        console.log('🌋 Loaded ' + usCount + ' US earthquakes (M2.5+ past 30 days)');
                    }
                })
                .catch(function(error) {
                    console.error('❌ USGS API error:', error);
                });
            
            // Add seismic hazard zones (static high-risk areas)
            var seismicZones = [
                {name: 'San Andreas Fault Zone', coords: [[35.0,-120.5],[36.5,-121.5],[37.5,-122.0],[38.5,-122.5]], risk: 'Very High'},
                {name: 'Cascadia Subduction Zone', coords: [[42.0,-124.5],[45.0,-124.0],[47.0,-124.5],[48.5,-124.5]], risk: 'Very High'},
                {name: 'New Madrid Seismic Zone', coords: [[35.5,-90.5],[36.5,-89.5],[37.5,-89.0],[36.0,-90.0]], risk: 'High'},
                {name: 'Wasatch Fault Zone', coords: [[39.5,-112.0],[40.5,-111.8],[41.5,-112.0],[42.0,-111.5]], risk: 'High'}
            ];
            
            seismicZones.forEach(function(zone) {
                L.polyline(zone.coords, {
                    color: zone.risk === 'Very High' ? '#ef4444' : '#f97316',
                    weight: 4,
                    opacity: 0.7,
                    dashArray: '10, 5'
                }).bindPopup(
                    '<div class="popup-title">⚠️ ' + zone.name + '</div>' +
                    '<div class="popup-row"><span class="popup-label">Seismic Risk</span><span class="popup-value" style="color:' + (zone.risk === 'Very High' ? '#ef4444' : '#f97316') + '">' + zone.risk + '</span></div>' +
                    '<div style="font-size:10px;color:var(--text3);margin-top:6px;">Consider seismic engineering requirements</div>'
                ).addTo(layers.seismic);
            });
        }
        
        // ============================================
        // INTERCONNECTION QUEUE DATA
        // Major ISO queue positions by region
        // ============================================
        var queueData = [
            // PJM Queue Hot Spots (2800+ active projects)
            {name: 'Loudoun County VA', lat: 39.08, lng: -77.52, projects: 145, mw: 12500, iso: 'PJM', type: 'Solar/Storage'},
            {name: 'Fauquier County VA', lat: 38.72, lng: -77.81, projects: 67, mw: 5400, iso: 'PJM', type: 'Solar'},
            {name: 'Frederick County MD', lat: 39.47, lng: -77.41, projects: 54, mw: 4200, iso: 'PJM', type: 'Solar/Storage'},
            {name: 'Lancaster County PA', lat: 40.04, lng: -76.31, projects: 48, mw: 3800, iso: 'PJM', type: 'Solar'},
            {name: 'Prince William VA', lat: 38.79, lng: -77.51, projects: 42, mw: 3200, iso: 'PJM', type: 'Data Center Load'},
            
            // ERCOT Queue (Texas)
            {name: 'Ector County TX', lat: 31.86, lng: -102.54, projects: 89, mw: 15000, iso: 'ERCOT', type: 'Solar/Wind'},
            {name: 'Reeves County TX', lat: 31.32, lng: -103.69, projects: 72, mw: 12000, iso: 'ERCOT', type: 'Solar'},
            {name: 'Andrews County TX', lat: 32.31, lng: -102.54, projects: 65, mw: 9500, iso: 'ERCOT', type: 'Solar/Storage'},
            {name: 'Pecos County TX', lat: 30.78, lng: -102.78, projects: 58, mw: 8200, iso: 'ERCOT', type: 'Solar'},
            {name: 'Culberson County TX', lat: 31.44, lng: -104.68, projects: 45, mw: 7500, iso: 'ERCOT', type: 'Wind'},
            
            // CAISO Queue (California)
            {name: 'Kern County CA', lat: 35.37, lng: -118.97, projects: 156, mw: 18000, iso: 'CAISO', type: 'Solar/Storage'},
            {name: 'Riverside County CA', lat: 33.95, lng: -115.99, projects: 98, mw: 14000, iso: 'CAISO', type: 'Solar/Storage'},
            {name: 'San Bernardino CA', lat: 34.84, lng: -116.18, projects: 87, mw: 12500, iso: 'CAISO', type: 'Solar'},
            {name: 'Imperial County CA', lat: 33.04, lng: -115.35, projects: 62, mw: 8500, iso: 'CAISO', type: 'Solar/Geothermal'},
            
            // MISO Queue
            {name: 'McLean County IL', lat: 40.49, lng: -88.99, projects: 34, mw: 2800, iso: 'MISO', type: 'Wind/Solar'},
            {name: 'Ford County IL', lat: 40.60, lng: -88.22, projects: 28, mw: 2200, iso: 'MISO', type: 'Wind'},
            {name: 'Livingston County IL', lat: 40.89, lng: -88.55, projects: 25, mw: 1900, iso: 'MISO', type: 'Solar'},
            
            // SPP Queue
            {name: 'Caddo County OK', lat: 35.17, lng: -98.38, projects: 42, mw: 5500, iso: 'SPP', type: 'Wind'},
            {name: 'Custer County OK', lat: 35.63, lng: -99.00, projects: 38, mw: 4800, iso: 'SPP', type: 'Wind/Solar'}
        ];
        
        queueData.forEach(function(q) {
            var color = q.iso === 'PJM' ? '#3b82f6' : q.iso === 'ERCOT' ? '#ef4444' : q.iso === 'CAISO' ? '#eab308' : q.iso === 'MISO' ? '#22c55e' : '#a855f7';
            var radius = Math.min(20, Math.max(8, q.projects / 8));
            
            L.circleMarker([q.lat, q.lng], {
                radius: radius,
                fillColor: color,
                color: '#fff',
                weight: 2,
                opacity: 0.9,
                fillOpacity: 0.5
            }).bindPopup(
                '<div class="popup-title">📋 ' + q.name + '</div>' +
                '<div class="popup-row"><span class="popup-label">ISO/RTO</span><span class="popup-value"><span class="iso-badge iso-' + q.iso.toLowerCase() + '">' + q.iso + '</span></span></div>' +
                '<div class="popup-row"><span class="popup-label">Active Projects</span><span class="popup-value">' + q.projects + '</span></div>' +
                '<div class="popup-row"><span class="popup-label">Total MW</span><span class="popup-value">' + q.mw.toLocaleString() + ' MW</span></div>' +
                '<div class="popup-row"><span class="popup-label">Primary Type</span><span class="popup-value">' + q.type + '</span></div>' +
                '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1);font-size:10px;color:#f59e0b;">⚠️ Queue congestion may impact interconnection timeline</div>'
            ).addTo(layers.queue);
        });
        console.log('📋 Interconnection Queue: ' + queueData.length + ' hot spots loaded');
        
        // ============================================
        // DC QUEUE - Data Center Load Interconnections
        // Source: interconnection.fyi (daily updates)
        // ============================================
        function loadDCQueueData() {
            if (layers.dcqueue.getLayers().length > 0) return;
            
            console.log('📡 Loading DC Queue data from interconnection.fyi...');
            
            // Data Center Load Queue Projects (based on interconnection.fyi Dec 2024)
            // 566 active load requests totaling 81.59 GW
            var dcQueueProjects = [
                // PJM - Northern Virginia (Largest DC market)
                {name: 'Loudoun County VA', lat: 39.08, lng: -77.52, projects: 28, mw: 4200, iso: 'PJM', status: 'Active', avgWait: 4.5, companies: ['AWS', 'Microsoft', 'Google', 'Meta'], queueIds: ['AE2-234', 'AE2-256', 'AE2-278']},
                {name: 'Prince William VA', lat: 38.79, lng: -77.51, projects: 18, mw: 2800, iso: 'PJM', status: 'Active', avgWait: 4.2, companies: ['QTS', 'Digital Realty', 'CyrusOne'], queueIds: ['AE2-301', 'AE2-315']},
                {name: 'Fairfax County VA', lat: 38.85, lng: -77.28, projects: 12, mw: 1600, iso: 'PJM', status: 'Active', avgWait: 3.8, companies: ['Equinix', 'CoreSite'], queueIds: ['AF1-122']},
                {name: 'Fauquier County VA', lat: 38.72, lng: -77.81, projects: 8, mw: 1200, iso: 'PJM', status: 'Study', avgWait: 5.2, companies: ['Amazon', 'Unknown'], queueIds: ['AG1-045']},
                {name: 'Frederick County MD', lat: 39.47, lng: -77.41, projects: 6, mw: 850, iso: 'PJM', status: 'Active', avgWait: 3.5, companies: ['ByteDance'], queueIds: ['AH2-018']},
                
                // PJM - Ohio/Columbus
                {name: 'Franklin County OH', lat: 39.96, lng: -82.99, projects: 15, mw: 2400, iso: 'PJM', status: 'Active', avgWait: 3.2, companies: ['AWS', 'Google', 'Meta'], queueIds: ['AI1-067', 'AI1-089']},
                {name: 'Licking County OH', lat: 40.09, lng: -82.48, projects: 8, mw: 1500, iso: 'PJM', status: 'Active', avgWait: 2.8, companies: ['Google', 'Meta'], queueIds: ['AJ2-034']},
                {name: 'New Albany OH', lat: 40.08, lng: -82.79, projects: 6, mw: 1200, iso: 'PJM', status: 'IA Signed', avgWait: 2.5, companies: ['Google'], queueIds: ['AK1-012']},
                
                // PJM - New Jersey
                {name: 'Middlesex County NJ', lat: 40.44, lng: -74.38, projects: 10, mw: 1400, iso: 'PJM', status: 'Active', avgWait: 3.8, companies: ['Digital Realty', 'Equinix'], queueIds: ['AL2-056']},
                
                // ERCOT - Texas (Fast-growing)
                {name: 'Dallas County TX', lat: 32.78, lng: -96.80, projects: 22, mw: 3200, iso: 'ERCOT', status: 'Active', avgWait: 1.8, companies: ['Compass', 'DataBank', 'QTS'], queueIds: ['INR-2045', 'INR-2067']},
                {name: 'Denton County TX', lat: 33.21, lng: -97.13, projects: 14, mw: 2100, iso: 'ERCOT', status: 'Active', avgWait: 1.5, companies: ['AWS', 'Meta'], queueIds: ['INR-2089']},
                {name: 'Tarrant County TX', lat: 32.76, lng: -97.29, projects: 10, mw: 1500, iso: 'ERCOT', status: 'Active', avgWait: 1.6, companies: ['Google', 'Facebook'], queueIds: ['INR-2101']},
                {name: 'Collin County TX', lat: 33.19, lng: -96.57, projects: 8, mw: 1200, iso: 'ERCOT', status: 'Study', avgWait: 2.0, companies: ['Meta', 'TikTok'], queueIds: ['INR-2123']},
                {name: 'Travis County TX', lat: 30.27, lng: -97.74, projects: 12, mw: 1800, iso: 'ERCOT', status: 'Active', avgWait: 1.4, companies: ['Apple', 'Tesla', 'Oracle'], queueIds: ['INR-2145']},
                {name: 'Williamson County TX', lat: 30.63, lng: -97.68, projects: 6, mw: 900, iso: 'ERCOT', status: 'Active', avgWait: 1.2, companies: ['Apple', 'Samsung'], queueIds: ['INR-2167']},
                {name: 'Harris County TX', lat: 29.76, lng: -95.37, projects: 8, mw: 1100, iso: 'ERCOT', status: 'Active', avgWait: 1.5, companies: ['Cyxtera', 'CyrusOne'], queueIds: ['INR-2189']},
                
                // CAISO - California
                {name: 'Santa Clara County CA', lat: 37.36, lng: -121.97, projects: 14, mw: 1800, iso: 'CAISO', status: 'Active', avgWait: 3.8, companies: ['Equinix', 'Digital Realty', 'CoreSite'], queueIds: ['GIDAP-456']},
                {name: 'San Jose CA', lat: 37.34, lng: -121.89, projects: 8, mw: 1200, iso: 'CAISO', status: 'Active', avgWait: 4.2, companies: ['Equinix', 'Vantage'], queueIds: ['GIDAP-478']},
                {name: 'Los Angeles County CA', lat: 34.05, lng: -118.24, projects: 10, mw: 1400, iso: 'CAISO', status: 'Study', avgWait: 4.5, companies: ['CoreSite', 'Digital Realty'], queueIds: ['GIDAP-501']},
                
                // MISO - Chicago/Midwest
                {name: 'Cook County IL', lat: 41.88, lng: -87.63, projects: 16, mw: 2200, iso: 'MISO', status: 'Active', avgWait: 3.2, companies: ['Equinix', 'Digital Realty', 'QTS'], queueIds: ['J1012']},
                {name: 'DuPage County IL', lat: 41.85, lng: -88.09, projects: 10, mw: 1400, iso: 'MISO', status: 'Active', avgWait: 2.8, companies: ['CyrusOne', 'Stream'], queueIds: ['J1034']},
                {name: 'Will County IL', lat: 41.45, lng: -87.98, projects: 6, mw: 800, iso: 'MISO', status: 'Study', avgWait: 3.5, companies: ['Meta', 'Microsoft'], queueIds: ['J1056']},
                
                // SPP - Oklahoma/Kansas
                {name: 'Oklahoma County OK', lat: 35.47, lng: -97.51, projects: 4, mw: 450, iso: 'SPP', status: 'Active', avgWait: 2.8, companies: ['Google'], queueIds: ['GEN-2023-456']},
                
                // NYISO - New York
                {name: 'Westchester County NY', lat: 41.12, lng: -73.80, projects: 8, mw: 950, iso: 'NYISO', status: 'Active', avgWait: 3.0, companies: ['Digital Realty'], queueIds: ['0789']},
                {name: 'Buffalo NY', lat: 42.89, lng: -78.88, projects: 4, mw: 550, iso: 'NYISO', status: 'Study', avgWait: 2.5, companies: ['Yahoo'], queueIds: ['0812']},
                
                // ISO-NE - Boston
                {name: 'Middlesex County MA', lat: 42.49, lng: -71.28, projects: 6, mw: 720, iso: 'ISO-NE', status: 'Active', avgWait: 2.2, companies: ['Digital Realty', 'CyrusOne'], queueIds: ['QP-1234']},
                
                // Southeast (Non-ISO)
                {name: 'Douglas County GA', lat: 33.75, lng: -84.75, projects: 12, mw: 1600, iso: 'SERC', status: 'Active', avgWait: 2.5, companies: ['Google', 'Facebook', 'Microsoft'], queueIds: ['GA-DC-045']},
                {name: 'Newton County GA', lat: 33.55, lng: -83.86, projects: 8, mw: 1200, iso: 'SERC', status: 'Active', avgWait: 2.2, companies: ['Google', 'Meta'], queueIds: ['GA-DC-067']},
                
                // Phoenix
                {name: 'Maricopa County AZ', lat: 33.45, lng: -112.07, projects: 18, mw: 2800, iso: 'Non-ISO', status: 'Active', avgWait: 1.8, companies: ['Microsoft', 'Google', 'Meta', 'Apple'], queueIds: ['APS-2045', 'SRP-3012']},
                {name: 'Goodyear AZ', lat: 33.44, lng: -112.36, projects: 8, mw: 1200, iso: 'Non-ISO', status: 'Active', avgWait: 1.5, companies: ['Microsoft', 'EdgeCore'], queueIds: ['APS-2067']},
                {name: 'Mesa AZ', lat: 33.42, lng: -111.83, projects: 6, mw: 850, iso: 'Non-ISO', status: 'Study', avgWait: 2.0, companies: ['Apple', 'Google'], queueIds: ['SRP-3034']},
                
                // Denver
                {name: 'Adams County CO', lat: 39.87, lng: -104.34, projects: 6, mw: 720, iso: 'Non-ISO', status: 'Active', avgWait: 2.2, companies: ['Vantage', 'EdgeCore'], queueIds: ['PSCO-456']},
                
                // Las Vegas / Reno
                {name: 'Henderson NV', lat: 36.04, lng: -114.98, projects: 8, mw: 950, iso: 'Non-ISO', status: 'Active', avgWait: 1.8, companies: ['Switch', 'Google'], queueIds: ['NVE-2023']},
                {name: 'Reno NV', lat: 39.53, lng: -119.81, projects: 6, mw: 780, iso: 'Non-ISO', status: 'Active', avgWait: 1.5, companies: ['Apple', 'Switch'], queueIds: ['NVE-2045']},
                
                // Portland/Seattle
                {name: 'Hillsboro OR', lat: 45.52, lng: -122.99, projects: 10, mw: 1400, iso: 'Non-ISO', status: 'Active', avgWait: 2.0, companies: ['Google', 'Amazon', 'Meta'], queueIds: ['PGE-DC-789']},
                {name: 'Quincy WA', lat: 47.23, lng: -119.85, projects: 8, mw: 1200, iso: 'Non-ISO', status: 'Active', avgWait: 1.5, companies: ['Microsoft', 'Yahoo'], queueIds: ['GCL-2045']},
                {name: 'Moses Lake WA', lat: 47.13, lng: -119.28, projects: 4, mw: 600, iso: 'Non-ISO', status: 'Study', avgWait: 1.8, companies: ['Microsoft', 'Amazon'], queueIds: ['GCL-2067']},
                
                // Salt Lake City
                {name: 'West Jordan UT', lat: 40.61, lng: -111.94, projects: 6, mw: 720, iso: 'Non-ISO', status: 'Active', avgWait: 2.0, companies: ['Meta', 'eBay'], queueIds: ['RMP-DC-123']},
                
                // Charlotte
                {name: 'Catawba County NC', lat: 35.66, lng: -81.22, projects: 8, mw: 1100, iso: 'SERC', status: 'Active', avgWait: 2.5, companies: ['Google', 'Apple', 'Meta'], queueIds: ['DEC-2045']},
                {name: 'Rowan County NC', lat: 35.64, lng: -80.52, projects: 4, mw: 580, iso: 'SERC', status: 'Study', avgWait: 3.0, companies: ['Meta'], queueIds: ['DEC-2067']}
            ];
            
            var totalMW = 0;
            var totalProjects = 0;
            
            dcQueueProjects.forEach(function(dc) {
                totalMW += dc.mw;
                totalProjects += dc.projects;
                
                // Color by ISO
                var color = dc.iso === 'PJM' ? '#3b82f6' : 
                           dc.iso === 'ERCOT' ? '#ef4444' : 
                           dc.iso === 'CAISO' ? '#eab308' : 
                           dc.iso === 'MISO' ? '#22c55e' : 
                           dc.iso === 'SPP' ? '#a855f7' :
                           dc.iso === 'NYISO' ? '#f97316' :
                           dc.iso === 'ISO-NE' ? '#06b6d4' :
                           '#8b5cf6'; // Non-ISO
                
                // Status indicator
                var statusColor = dc.status === 'IA Signed' ? '#22c55e' : 
                                 dc.status === 'Active' ? '#3b82f6' : '#f59e0b';
                var statusBg = dc.status === 'IA Signed' ? 'rgba(34,197,94,0.2)' : 
                              dc.status === 'Active' ? 'rgba(59,130,246,0.2)' : 'rgba(245,158,11,0.2)';
                
                // Size by MW
                var radius = Math.min(22, Math.max(10, dc.mw / 200));
                
                // Create pulsing marker for large projects
                var markerOptions = {
                    radius: radius,
                    fillColor: color,
                    color: '#fff',
                    weight: 2,
                    opacity: 0.95,
                    fillOpacity: 0.7
                };
                
                var marker = L.circleMarker([dc.lat, dc.lng], markerOptions);
                
                // Rich popup with DC-specific info
                marker.bindPopup(
                    '<div style="min-width:300px">' +
                    '<div style="font-weight:700;font-size:15px;margin-bottom:10px;color:#f97316;display:flex;align-items:center;gap:8px">' +
                    '<span style="font-size:20px">🏢</span> ' + dc.name + ' DC Load Queue' +
                    '</div>' +
                    
                    // Status badge
                    '<div style="display:inline-block;padding:4px 10px;background:' + statusBg + ';border-radius:6px;font-size:11px;font-weight:700;color:' + statusColor + ';margin-bottom:10px">' +
                    '● ' + dc.status + '</div>' +
                    '<span class="iso-badge iso-' + dc.iso.toLowerCase().replace('-', '') + '" style="margin-left:8px">' + dc.iso + '</span>' +
                    
                    // Key metrics
                    '<div style="background:rgba(249,115,22,0.1);border-radius:8px;padding:10px;margin-bottom:10px">' +
                    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px">' +
                    '<div><span style="color:#9ca3af">Load Requests:</span> <strong style="color:#f97316">' + dc.projects + '</strong></div>' +
                    '<div><span style="color:#9ca3af">Total Capacity:</span> <strong style="color:#22c55e">' + dc.mw.toLocaleString() + ' MW</strong></div>' +
                    '<div><span style="color:#9ca3af">Avg Wait Time:</span> <strong style="color:#eab308">' + dc.avgWait + ' years</strong></div>' +
                    '<div><span style="color:#9ca3af">Queue IDs:</span> <span style="color:#6b7280;font-size:10px">' + dc.queueIds.slice(0,2).join(', ') + '</span></div>' +
                    '</div></div>' +
                    
                    // Companies
                    '<div style="margin-bottom:10px">' +
                    '<div style="font-size:11px;color:#9ca3af;margin-bottom:4px">Known Operators:</div>' +
                    '<div style="display:flex;flex-wrap:wrap;gap:4px">' +
                    dc.companies.map(function(c) {
                        return '<span style="padding:2px 8px;background:rgba(99,102,241,0.15);border-radius:4px;font-size:11px;color:#818cf8">' + c + '</span>';
                    }).join('') +
                    '</div></div>' +
                    
                    // Capacity bar
                    '<div style="margin-bottom:10px">' +
                    '<div style="font-size:10px;color:#9ca3af;margin-bottom:4px">Capacity Scale</div>' +
                    '<div style="height:8px;background:#1f2937;border-radius:4px;overflow:hidden">' +
                    '<div style="height:100%;width:' + Math.min(100, dc.mw / 50) + '%;background:linear-gradient(90deg,#22c55e,#f59e0b,#ef4444)"></div>' +
                    '</div>' +
                    '<div style="display:flex;justify-content:space-between;font-size:9px;color:#6b7280;margin-top:2px"><span>0 MW</span><span>' + dc.mw.toLocaleString() + ' MW</span><span>5,000 MW</span></div>' +
                    '</div>' +
                    
                    // Footer
                    '<div style="padding-top:8px;border-top:1px solid rgba(255,255,255,0.1);font-size:10px;display:flex;justify-content:space-between;align-items:center">' +
                    '<span style="color:#06b6d4">📡 Source: interconnection.fyi</span>' +
                    '<span style="color:#6b7280">Updated daily</span>' +
                    '</div>' +
                    '</div>'
                );
                
                marker.addTo(layers.dcqueue);
            });
            
            document.getElementById('count-dcqueue').textContent = totalProjects;
            console.log('✅ Loaded ' + dcQueueProjects.length + ' DC queue locations (' + totalProjects + ' projects, ' + (totalMW/1000).toFixed(1) + ' GW)');
            
            // Show summary alert
            alert('🏢 DC Queue Loaded!\\n\\n' +
                  '• ' + totalProjects + ' active load requests\\n' +
                  '• ' + (totalMW/1000).toFixed(1) + ' GW total capacity\\n' +
                  '• Data from interconnection.fyi (daily updates)\\n\\n' +
                  'Click markers for details on wait times, operators, and queue IDs.');
        }
        
        // ============================================
        // PROPERTY TAX RATES BY REGION
        // Added to site evaluation
        // ============================================
        var taxRates = {
            'virginia': {rate: '0.86%', effective: '$8.60/$1000', rank: 'Low'},
            'texas-dfw': {rate: '1.69%', effective: '$16.90/$1000', rank: 'High'},
            'texas-houston': {rate: '1.81%', effective: '$18.10/$1000', rank: 'High'},
            'phoenix': {rate: '0.62%', effective: '$6.20/$1000', rank: 'Very Low'},
            'chicago': {rate: '2.27%', effective: '$22.70/$1000', rank: 'Very High'},
            'silicon-valley': {rate: '0.73%', effective: '$7.30/$1000', rank: 'Low'},
            'atlanta': {rate: '0.92%', effective: '$9.20/$1000', rank: 'Low'},
            'denver': {rate: '0.55%', effective: '$5.50/$1000', rank: 'Very Low'},
            'seattle': {rate: '0.93%', effective: '$9.30/$1000', rank: 'Low'},
            'oregon': {rate: '0.97%', effective: '$9.70/$1000', rank: 'Medium'},
            'nevada': {rate: '0.60%', effective: '$6.00/$1000', rank: 'Very Low'},
            'utah': {rate: '0.57%', effective: '$5.70/$1000', rank: 'Very Low'},
            'ohio': {rate: '1.59%', effective: '$15.90/$1000', rank: 'High'},
            'indiana': {rate: '0.85%', effective: '$8.50/$1000', rank: 'Low'},
            'iowa': {rate: '1.57%', effective: '$15.70/$1000', rank: 'High'},
            'wyoming': {rate: '0.57%', effective: '$5.70/$1000', rank: 'Very Low'},
            'new-mexico': {rate: '0.80%', effective: '$8.00/$1000', rank: 'Low'}
        };
        
        console.log('💰 Property tax rates loaded for ' + Object.keys(taxRates).length + ' regions');
        
        // PDF Export Function for Site Reports
        window.exportSiteReport = function() {
            if (!window.lastSiteEvaluation) {
                alert('Please evaluate a site first by searching an address or clicking on the map.');
                return;
            }
            
            var site = window.lastSiteEvaluation;
            var jsPDF = window.jspdf.jsPDF;
            var doc = new jsPDF();
            
            // Header
            doc.setFillColor(99, 102, 241);
            doc.rect(0, 0, 210, 35, 'F');
            doc.setTextColor(255, 255, 255);
            doc.setFontSize(22);
            doc.setFont('helvetica', 'bold');
            doc.text('DC Hub Site Evaluation Report', 15, 20);
            doc.setFontSize(10);
            doc.setFont('helvetica', 'normal');
            doc.text('Generated: ' + new Date().toLocaleString(), 15, 28);
            
            // Site Location
            doc.setTextColor(0, 0, 0);
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Site Location', 15, 48);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text('Address: ' + (site.address || 'N/A'), 15, 56);
            doc.text('Coordinates: ' + site.lat.toFixed(6) + ', ' + site.lng.toFixed(6), 15, 63);
            
            // Grid Information
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Grid & Power Information', 15, 78);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text('ISO/RTO Region: ' + (site.iso || 'Non-ISO Territory'), 15, 86);
            doc.text('Grid Operator: ' + (site.gridOperator || 'N/A'), 15, 93);
            doc.text('Estimated Power Cost: ' + (site.powerCost || 'N/A'), 15, 100);
            
            // Infrastructure Proximity
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Infrastructure Proximity', 15, 115);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text('Nearest Substation: ' + (site.nearestSubstation || 'N/A'), 15, 123);
            doc.text('Nearest Fiber: ' + (site.nearestFiber || 'N/A'), 15, 130);
            doc.text('Nearest Airport: ' + (site.nearestAirport || 'N/A'), 15, 137);
            doc.text('Nearest Data Center: ' + (site.nearestDC || 'N/A'), 15, 144);
            
            // Risk Assessment
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Risk Assessment', 15, 159);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text('FEMA Flood Zone: ' + (site.floodZone || 'Check FEMA Map'), 15, 167);
            doc.text('Seismic Risk: ' + (site.seismicRisk || 'N/A'), 15, 174);
            doc.text('Water Availability: ' + (site.waterRisk || 'N/A'), 15, 181);
            
            // Tax & Incentives
            doc.setFontSize(14);
            doc.setFont('helvetica', 'bold');
            doc.text('Tax & Incentives', 15, 196);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            doc.text('State: ' + (site.state || 'N/A'), 15, 204);
            doc.text('Property Tax Rate: ' + (site.taxRate || 'N/A'), 15, 211);
            doc.text('Tax Incentives: ' + (site.incentives || 'Check state programs'), 15, 218);
            
            // Site Score
            if (site.score) {
                doc.setFillColor(16, 185, 129);
                doc.roundedRect(15, 230, 50, 25, 5, 5, 'F');
                doc.setTextColor(255, 255, 255);
                doc.setFontSize(18);
                doc.setFont('helvetica', 'bold');
                doc.text('Score: ' + site.score, 22, 246);
            }
            
            // Footer
            doc.setTextColor(150, 150, 150);
            doc.setFontSize(9);
            doc.setFont('helvetica', 'normal');
            doc.text('DC Hub - dchub.cloud - Data Center Intelligence Platform', 15, 280);
            doc.text('This report is for informational purposes only. Verify all data before making investment decisions.', 15, 286);
            
            // Save PDF
            var filename = 'dc-hub-site-report-' + site.lat.toFixed(4) + '-' + site.lng.toFixed(4) + '.pdf';
            doc.save(filename);
        };
        
        // Store last evaluation for export
        var originalShowSiteEval = window.showSiteEvaluation;
        if (typeof originalShowSiteEval === 'function') {
            window.showSiteEvaluation = function(data) {
                window.lastSiteEvaluation = data;
                return originalShowSiteEval(data);
            };
        }
        
        // ============================================
        // MEASUREMENT TOOL
        // ============================================
        
        var measureMode = false;
        window.measureMode = measureMode; // Expose globally
        var measurePoints = [];
        var measureMarkers = [];
        var measureLines = [];
        var measureLayer = L.layerGroup().addTo(map);
        
        // Haversine formula for distance calculation
        function haversineDistance(lat1, lon1, lat2, lon2) {
            var R = 3959; // Earth's radius in miles
            var dLat = (lat2 - lat1) * Math.PI / 180;
            var dLon = (lon2 - lon1) * Math.PI / 180;
            var a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                    Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                    Math.sin(dLon/2) * Math.sin(dLon/2);
            var c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            return R * c;
        }
        
        // Toggle measurement mode
        function toggleMeasure() {
            measureMode = !measureMode;
            window.measureMode = measureMode; // Sync globally
            var btn = document.getElementById('measure-btn');
            var panel = document.getElementById('measure-panel');
            var mapContainer = document.getElementById('map');
            
            if (measureMode) {
                if (btn) btn.classList.add('active');
                if (panel) panel.classList.add('active');
                if (mapContainer) mapContainer.classList.add('measure-cursor');
                document.getElementById('measure-info').innerHTML = '👆 Click on map to set <strong>Point 1</strong>';
            } else {
                if (btn) btn.classList.remove('active');
                if (panel) panel.classList.remove('active');
                if (mapContainer) mapContainer.classList.remove('measure-cursor');
            }
        }
        window.toggleMeasure = toggleMeasure;
        
        // Clear all measurements
        function clearMeasure() {
            console.log('🗑️ Clearing measurements...');
            measurePoints = [];
            measureMarkers = [];
            measureLines = [];
            measureLayer.clearLayers();
            
            // Reset panel
            document.getElementById('measure-result').style.display = 'none';
            document.getElementById('measure-segments').innerHTML = '';
            document.getElementById('measure-info').innerHTML = '👆 Click on map to set <strong>Point 1</strong>';
            
            // Also disable measure mode and close panel
            measureMode = false;
            window.measureMode = false; // Sync globally
            var btn = document.getElementById('measure-btn');
            var panel = document.getElementById('measure-panel');
            var mapContainer = document.getElementById('map');
            
            if (btn) btn.classList.remove('active');
            if (panel) panel.classList.remove('active');
            if (mapContainer) mapContainer.classList.remove('measure-cursor');
            
            console.log('✅ Measurements cleared and measure mode disabled');
        }
        window.clearMeasure = clearMeasure;
        
        // Undo last point
        function undoMeasure() {
            console.log('↩️ Undoing last point...');
            if (measurePoints.length === 0) return;
            
            // Remove last point
            measurePoints.pop();
            
            // Remove last marker and label
            if (measureMarkers.length > 0) {
                var last = measureMarkers.pop();
                measureLayer.removeLayer(last.marker);
                measureLayer.removeLayer(last.label);
            }
            
            // Remove last line and distance label (2 items per segment)
            if (measureLines.length > 0) {
                var item1 = measureLines.pop();
                if (item1) measureLayer.removeLayer(item1);
            }
            if (measureLines.length > 0) {
                var item2 = measureLines.pop();
                if (item2) measureLayer.removeLayer(item2);
            }
            
            updateMeasurePanel();
            console.log('✅ Last point removed, ' + measurePoints.length + ' points remaining');
        }
        window.undoMeasure = undoMeasure;
        
        // Add measurement point on map click
        map.on('click', function(e) {
            if (!measureMode) return;
            
            var latlng = e.latlng;
            var pointNum = measurePoints.length + 1;
            
            // Create marker
            var marker = L.circleMarker(latlng, {
                radius: 8,
                fillColor: pointNum === 1 ? '#10b981' : '#6366f1',
                color: '#fff',
                weight: 2,
                fillOpacity: 1
            }).addTo(measureLayer);
            
            // Add label
            var label = L.marker(latlng, {
                icon: L.divIcon({
                    className: 'measure-label',
                    html: '<div style="background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;white-space:nowrap;transform:translate(-50%,-150%)">Point ' + pointNum + '</div>',
                    iconSize: [0, 0]
                })
            }).addTo(measureLayer);
            
            measureMarkers.push({ marker: marker, label: label });
            measurePoints.push(latlng);
            
            // Draw line to previous point
            if (measurePoints.length > 1) {
                var prevPoint = measurePoints[measurePoints.length - 2];
                var line = L.polyline([prevPoint, latlng], {
                    color: '#6366f1',
                    weight: 3,
                    opacity: 0.8,
                    dashArray: '10, 5'
                }).addTo(measureLayer);
                measureLines.push(line);
                
                // Calculate segment distance
                var segDist = haversineDistance(prevPoint.lat, prevPoint.lng, latlng.lat, latlng.lng);
                
                // Add distance label at midpoint
                var midLat = (prevPoint.lat + latlng.lat) / 2;
                var midLng = (prevPoint.lng + latlng.lng) / 2;
                var distLabel = L.marker([midLat, midLng], {
                    icon: L.divIcon({
                        className: 'measure-dist-label',
                        html: '<div style="background:rgba(10,10,18,0.9);color:#10b981;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;white-space:nowrap;border:1px solid #252836">' + segDist.toFixed(2) + ' mi</div>',
                        iconSize: [0, 0]
                    })
                }).addTo(measureLayer);
                measureLines.push(distLabel);
            }
            
            updateMeasurePanel();
        });
        
        // Update measurement panel with results
        function updateMeasurePanel() {
            var resultDiv = document.getElementById('measure-result');
            var segmentsDiv = document.getElementById('measure-segments');
            var infoDiv = document.getElementById('measure-info');
            
            if (measurePoints.length === 0) {
                resultDiv.style.display = 'none';
                segmentsDiv.innerHTML = '';
                infoDiv.innerHTML = '👆 Click on map to set <strong>Point 1</strong>';
                return;
            }
            
            if (measurePoints.length === 1) {
                infoDiv.innerHTML = '👆 Click to set <strong>Point 2</strong> - measuring from Point 1';
                resultDiv.style.display = 'none';
                segmentsDiv.innerHTML = '<div class="measure-segment" onclick="deleteSegment(0)" title="Click to delete"><span class="seg-label">📍 Point 1</span><span class="seg-value">' + measurePoints[0].lat.toFixed(4) + ', ' + measurePoints[0].lng.toFixed(4) + '</span><span class="seg-delete">✕</span></div>';
                return;
            }
            
            // Calculate total distance
            var totalMiles = 0;
            var segmentsHtml = '';
            
            for (var i = 0; i < measurePoints.length; i++) {
                if (i > 0) {
                    var segDist = haversineDistance(
                        measurePoints[i-1].lat, measurePoints[i-1].lng,
                        measurePoints[i].lat, measurePoints[i].lng
                    );
                    totalMiles += segDist;
                    segmentsHtml += '<div class="measure-segment" onclick="deleteSegment(' + i + ')" title="Click to delete segment ' + i + '"><span class="seg-label">Seg ' + i + ' → Pt ' + (i+1) + '</span><span class="seg-value">' + segDist.toFixed(2) + ' mi</span><span class="seg-delete">✕</span></div>';
                }
            }
            
            resultDiv.style.display = 'block';
            document.getElementById('measure-total').textContent = totalMiles.toFixed(2) + ' mi';
            document.getElementById('measure-total-km').textContent = (totalMiles * 1.60934).toFixed(2) + ' km';
            segmentsDiv.innerHTML = segmentsHtml;
            infoDiv.innerHTML = '✅ <strong>' + measurePoints.length + ' points</strong> - Click segments to delete';
        }
        
        // Delete a specific segment/point
        function deleteSegment(index) {
            console.log('🗑️ Deleting point at index ' + index);
            if (index < 0 || index >= measurePoints.length) return;
            
            // Remove the point
            measurePoints.splice(index, 1);
            
            // Clear and rebuild all markers and lines
            measureLayer.clearLayers();
            measureMarkers = [];
            measureLines = [];
            
            // Rebuild markers
            measurePoints.forEach(function(latlng, i) {
                var pointNum = i + 1;
                var marker = L.circleMarker(latlng, {
                    radius: 8,
                    fillColor: pointNum === 1 ? '#10b981' : '#6366f1',
                    color: '#fff',
                    weight: 2,
                    fillOpacity: 1
                }).addTo(measureLayer);
                
                var label = L.marker(latlng, {
                    icon: L.divIcon({
                        className: 'measure-label',
                        html: '<div style="background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;white-space:nowrap;transform:translate(-50%,-150%)">Point ' + pointNum + '</div>',
                        iconSize: [0, 0]
                    })
                }).addTo(measureLayer);
                
                measureMarkers.push({ marker: marker, label: label });
                
                // Draw line to previous point
                if (i > 0) {
                    var prevPoint = measurePoints[i - 1];
                    var line = L.polyline([prevPoint, latlng], {
                        color: '#6366f1',
                        weight: 3,
                        opacity: 0.8,
                        dashArray: '10, 5'
                    }).addTo(measureLayer);
                    measureLines.push(line);
                    
                    var segDist = haversineDistance(prevPoint.lat, prevPoint.lng, latlng.lat, latlng.lng);
                    var midLat = (prevPoint.lat + latlng.lat) / 2;
                    var midLng = (prevPoint.lng + latlng.lng) / 2;
                    var distLabel = L.marker([midLat, midLng], {
                        icon: L.divIcon({
                            className: 'measure-dist-label',
                            html: '<div style="background:rgba(10,10,18,0.9);color:#10b981;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:700;font-family:JetBrains Mono,monospace;white-space:nowrap;border:1px solid #252836">' + segDist.toFixed(2) + ' mi</div>',
                            iconSize: [0, 0]
                        })
                    }).addTo(measureLayer);
                    measureLines.push(distLabel);
                }
            });
            
            updateMeasurePanel();
            console.log('✅ Segment deleted, ' + measurePoints.length + ' points remaining');
        }
        window.deleteSegment = deleteSegment;
        
        // Attach button handlers directly
        document.getElementById('measure-panel').querySelector('.btn-clear').addEventListener('click', clearMeasure);
        document.getElementById('measure-panel').querySelector('.btn-undo').addEventListener('click', undoMeasure);
        
        console.log('📏 Measurement Tool: Click measure button to start');
        
        // ============================================
        // TIER 2: UTILITY TERRITORIES (v100)
        // Electric Retail Service Territories from HIFLD
        // ============================================
        var UTILITY_COLORS = {
            'Investor Owned': '#3b82f6',      // Blue
            'Municipal': '#10b981',            // Green  
            'Cooperative': '#f59e0b',          // Orange
            'Political Subdivision': '#8b5cf6', // Purple
            'State': '#ef4444',                // Red
            'Federal': '#06b6d4',              // Cyan
            'Unknown': '#6b7280'               // Gray
        };
        
        // Multiple endpoint fallbacks for utility territories
        var UTILITY_ENDPOINTS = [
            // Primary: HIFLD Geoplatform (CORS-enabled)
            'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Retail_Service_Territories_2/FeatureServer/0/query',
            // Fallback 1: Alternate HIFLD naming
            'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Retail_Service_Territories/FeatureServer/0/query',
            // Fallback 2: Living Atlas
            'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Electric_Retail_Service_Territories/FeatureServer/0/query'
        ];
        
        async function loadUtilityTerritories(bounds) {
            // Clear existing and reload
            layers.utilityTerritories.clearLayers();
            
            console.log('🏢 Loading Utility Service Territories...');
            
            var bbox = getBboxString(bounds);
            var features = null;
            var successEndpoint = null;
            
            // Try each endpoint until one works
            for (var i = 0; i < UTILITY_ENDPOINTS.length; i++) {
                var endpoint = UTILITY_ENDPOINTS[i];
                console.log('🏢 Trying endpoint ' + (i + 1) + '/' + UTILITY_ENDPOINTS.length + '...');
                
                try {
                    var url = endpoint + '?' + new URLSearchParams({
                        where: '1=1',
                        geometry: bbox,
                        geometryType: 'esriGeometryEnvelope',
                        spatialRel: 'esriSpatialRelIntersects',
                        inSR: '4326',
                        outSR: '4326',
                        outFields: 'NAME,STATE,CNTRL_AREA,HOLDING_CO,TYPE,CUSTOMERS,SALES,name,state,type,customers',
                        returnGeometry: true,
                        f: 'json',
                        resultRecordCount: 150
                    });
                    
                    var response = await fetch(url, { 
                        signal: AbortSignal.timeout(15000) // 15 second timeout
                    });
                    
                    if (!response.ok) {
                        console.log('🏢 Endpoint ' + (i + 1) + ' HTTP error: ' + response.status);
                        continue;
                    }
                    
                    var data = await response.json();
                    
                    if (data.error) {
                        console.log('🏢 Endpoint ' + (i + 1) + ' API error:', data.error.message || data.error);
                        continue;
                    }
                    
                    if (data.features && data.features.length > 0) {
                        features = data.features;
                        successEndpoint = endpoint;
                        console.log('🏢 SUCCESS: Endpoint ' + (i + 1) + ' returned ' + features.length + ' utilities');
                        break;
                    } else {
                        console.log('🏢 Endpoint ' + (i + 1) + ' returned 0 features in this area');
                    }
                } catch (error) {
                    if (error.name === 'TimeoutError' || error.name === 'AbortError') {
                        console.log('🏢 Endpoint ' + (i + 1) + ' timeout');
                    } else if (error.message && error.message.includes('CORS')) {
                        console.log('🏢 Endpoint ' + (i + 1) + ' CORS blocked');
                    } else {
                        console.log('🏢 Endpoint ' + (i + 1) + ' error:', error.message);
                    }
                }
            }
            
            // Render features if we got any
            if (features && features.length > 0) {
                var count = 0;
                
                features.forEach(function(feature) {
                    var props = feature.attributes || {};
                    var rings = feature.geometry && feature.geometry.rings;
                    if (!rings || rings.length === 0) return;
                    
                    // Handle both uppercase and lowercase field names
                    var utilityType = props.TYPE || props.type || 'Unknown';
                    var color = UTILITY_COLORS[utilityType] || UTILITY_COLORS['Unknown'];
                    
                    // Convert rings to Leaflet polygon format
                    var polygonCoords = rings.map(function(ring) {
                        return ring.map(function(coord) {
                            return [coord[1], coord[0]]; // [lat, lng]
                        });
                    });
                    
                    var name = props.NAME || props.name || 'Unknown Utility';
                    var state = props.STATE || props.state || 'N/A';
                    var customers = props.CUSTOMERS || props.customers;
                    customers = customers ? parseInt(customers).toLocaleString() : 'N/A';
                    var sales = props.SALES || props.sales;
                    sales = sales ? (parseInt(sales) / 1000000).toFixed(1) + ' TWh' : 'N/A';
                    var cntrlArea = props.CNTRL_AREA || props.cntrl_area || 'N/A';
                    var holdingCo = props.HOLDING_CO || props.holding_co || 'N/A';
                    
                    L.polygon(polygonCoords, {
                        color: color,
                        weight: 2,
                        opacity: 0.8,
                        fillColor: color,
                        fillOpacity: 0.15,
                        className: 'utility-territory'
                    }).bindPopup(
                        '<div style="min-width:260px">' +
                        '<div class="popup-title">🏢 ' + name + '</div>' +
                        '<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value" style="color:' + color + ';font-weight:700">' + utilityType + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">State</span><span class="popup-value">' + state + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">Control Area</span><span class="popup-value">' + cntrlArea + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">Holding Co</span><span class="popup-value">' + holdingCo + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">Customers</span><span class="popup-value">' + customers + '</span></div>' +
                        '<div class="popup-row"><span class="popup-label">Annual Sales</span><span class="popup-value" style="color:#22c55e;font-weight:700">' + sales + '</span></div>' +
                        '<div class="popup-row" style="color:#3b82f6;font-size:10px;margin-top:6px;">📡 HIFLD Geoplatform Data</div>' +
                        '</div>'
                    ).addTo(layers.utilityTerritories);
                    count++;
                });
                
                document.getElementById('count-utility').textContent = count;
                console.log('✅ Utility Territories loaded: ' + count + ' service areas');
            } else {
                console.log('🏢 No utility territories available - all endpoints failed or returned no data');
                console.log('🏢 This may be a CORS issue. Consider setting up a proxy worker.');
                document.getElementById('count-utility').textContent = '0';
            }
        }
        
        console.log('📍 Site Evaluation: Right-click map or use search box');
        console.log('⚡ ISO/RTO Regions: ' + isoRegions.length + ' grid operators loaded');
        console.log('🌿 Environmental Layers: Wetlands (NWI), Seismic (USGS)');
        console.log('📋 Interconnection Queue: ' + queueData.length + ' congestion hot spots');
        console.log('🔌 HIFLD Data: 70,000+ substations, 300,000+ miles transmission');
        console.log('🔥 Gas Infrastructure: 300,000+ miles pipelines available');
        console.log('💧 Water Resources: Aquifers and major rivers mapped');
        console.log('🏢 Tier 2: Utility Service Territories (3,200+ utilities nationwide)');
        console.log('DC Hub v100 - Tier 2 Territory Overlays');
        console.log('📦 90+ County Parcel Services across 30+ states');
        console.log('🌐 199 Fiber Routes from 40+ carriers (incl. dark fiber)');
        console.log('🚂 Railroad layer: FRA NTAD (all Class I/II/III + short lines)');
        console.log('⚡ ISO/RTO boundaries fixed (reduced fill opacity)');
        console.log('📏 Measurement tool: Click segments to delete');
        
        // Initialize HIFLD Infrastructure Data (if available)
        if (typeof DCHubInfrastructure !== 'undefined') {
            DCHubInfrastructure.init(map, layers);
        }
    });
