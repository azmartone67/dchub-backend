/**
 * DC Hub PDF Enhancement Module
 * ==============================
 * Adds Grid Demand, Fuel Mix, Energy Prices, EPA, and FCC data to site reports
 */

(function() {
    'use strict';
    
    const API_BASE = 'https://dchub.cloud';
    
    // ISO to state mapping for automatic detection
    const stateToISO = {
        'CA': 'CAISO', 'AZ': 'CAISO', 'NV': 'CAISO',
        'TX': 'ERCOT',
        'VA': 'PJM', 'MD': 'PJM', 'PA': 'PJM', 'NJ': 'PJM', 'OH': 'PJM', 'WV': 'PJM', 'DE': 'PJM', 'DC': 'PJM', 'IL': 'PJM', 'IN': 'PJM', 'KY': 'PJM', 'MI': 'PJM', 'NC': 'PJM',
        'NY': 'NYISO',
        'MN': 'MISO', 'WI': 'MISO', 'IA': 'MISO', 'MO': 'MISO', 'AR': 'MISO', 'LA': 'MISO', 'MS': 'MISO', 'ND': 'MISO', 'SD': 'MISO', 'MT': 'MISO',
        'OK': 'SPP', 'KS': 'SPP', 'NE': 'SPP', 'NM': 'SPP',
        'MA': 'ISONE', 'CT': 'ISONE', 'RI': 'ISONE', 'NH': 'ISONE', 'VT': 'ISONE', 'ME': 'ISONE',
        'GA': 'SERC', 'FL': 'SERC', 'AL': 'SERC', 'SC': 'SERC', 'TN': 'SERC',
        'WA': 'BPA', 'OR': 'BPA', 'ID': 'BPA'
    };
    
    /**
     * Fetch all enhanced data for a site
     */
    async function fetchEnhancedSiteData(lat, lng, state) {
        const data = {
            gridDemand: null,
            fuelMix: null,
            energyPrice: null,
            epaFacilities: null,
            broadband: null,
            iso: stateToISO[state] || 'Unknown'
        };
        
        // Fetch all data in parallel
        const promises = [];
        
        // Grid Demand
        if (data.iso && data.iso !== 'Unknown') {
            promises.push(
                fetch(`${API_BASE}/api/grid/demand?iso=${data.iso}`)
                    .then(r => r.json())
                    .then(d => { if (d.success) data.gridDemand = d.data; })
                    .catch(() => {})
            );
            
            // Fuel Mix
            promises.push(
                fetch(`${API_BASE}/api/grid/fuel-mix?iso=${data.iso}`)
                    .then(r => r.json())
                    .then(d => { if (d.success) data.fuelMix = d.data; })
                    .catch(() => {})
            );
        }
        
        // Energy Price
        if (state) {
            promises.push(
                fetch(`${API_BASE}/api/eia/prices?state=${state}`)
                    .then(r => r.json())
                    .then(d => { 
                        if (d.success && d.data && d.data.length > 0) {
                            data.energyPrice = d.data[0];
                        }
                    })
                    .catch(() => {})
            );
        }
        
        // EPA Facilities
        promises.push(
            fetch(`${API_BASE}/api/epa/facilities?lat=${lat}&lng=${lng}&radius=50`)
                .then(r => r.json())
                .then(d => { 
                    if (d.success && d.data) {
                        data.epaFacilities = {
                            total: d.data.total_in_state || 0,
                            nearby: d.data.facilities_in_radius?.length || 0,
                            state: d.data.state
                        };
                    }
                })
                .catch(() => {})
        );
        
        // FCC Broadband
        promises.push(
            fetch(`${API_BASE}/api/fcc/broadband?lat=${lat}&lng=${lng}`)
                .then(r => r.json())
                .then(d => { 
                    if (d.success && d.data) {
                        data.broadband = d.data;
                    }
                })
                .catch(() => {})
        );
        
        await Promise.all(promises);
        return data;
    }
    
    /**
     * Add enhanced sections to PDF document
     * @param {jsPDF} doc - The jsPDF document
     * @param {number} y - Current Y position
     * @param {object} enhancedData - Data from fetchEnhancedSiteData
     * @returns {number} - New Y position
     */
    function addEnhancedSectionsToPDF(doc, y, enhancedData) {
        const primary = [15, 118, 110];
        const secondary = [59, 130, 246];
        const text = [31, 41, 55];
        const lightBg = [249, 250, 251];
        const orange = [245, 158, 11];
        const green = [16, 185, 129];
        
        // Check if we need a new page
        if (y > 180) {
            doc.addPage();
            y = 20;
        }
        
        // ===========================================
        // GRID & ENERGY SECTION
        // ===========================================
        doc.setFillColor(...orange);
        doc.rect(15, y, 180, 8, 'F');
        doc.setTextColor(255, 255, 255);
        doc.setFontSize(12);
        doc.setFont('helvetica', 'bold');
        doc.text('⚡ Real-Time Grid & Energy Data', 20, y + 6);
        y += 12;
        
        doc.setTextColor(...text);
        doc.setFontSize(11);
        doc.setFont('helvetica', 'normal');
        
        // ISO and Demand
        if (enhancedData.iso && enhancedData.iso !== 'Unknown') {
            doc.text(`ISO/RTO Region: ${enhancedData.iso}`, 15, y);
            y += 6;
            
            if (enhancedData.gridDemand) {
                doc.setFont('helvetica', 'bold');
                doc.text(`Current Grid Demand: ${enhancedData.gridDemand.demand_gw?.toFixed(1) || 'N/A'} GW`, 15, y);
                doc.setFont('helvetica', 'normal');
                y += 6;
            }
        }
        
        // Energy Price
        if (enhancedData.energyPrice) {
            const price = enhancedData.energyPrice.price_cents_kwh;
            const priceColor = price < 10 ? green : price < 15 ? orange : [239, 68, 68];
            doc.setTextColor(...priceColor);
            doc.setFont('helvetica', 'bold');
            doc.text(`State Electricity Price: ${price?.toFixed(2)}¢/kWh (${enhancedData.energyPrice.year})`, 15, y);
            doc.setTextColor(...text);
            doc.setFont('helvetica', 'normal');
            y += 8;
        }
        
        // Fuel Mix Table
        if (enhancedData.fuelMix && enhancedData.fuelMix.fuel_mix) {
            y += 2;
            doc.setFontSize(10);
            doc.setFont('helvetica', 'bold');
            doc.text('Current Generation Fuel Mix:', 15, y);
            y += 6;
            
            // Table header
            doc.setFillColor(...secondary);
            doc.setTextColor(255, 255, 255);
            doc.rect(15, y, 85, 6, 'F');
            doc.setFontSize(9);
            doc.text('Fuel Source', 18, y + 4.5);
            doc.text('MW', 55, y + 4.5);
            doc.text('%', 78, y + 4.5);
            y += 6;
            
            doc.setTextColor(...text);
            doc.setFont('helvetica', 'normal');
            
            // Sort and display top fuels
            const fuelMix = enhancedData.fuelMix.fuel_mix;
            const sorted = Object.entries(fuelMix)
                .sort((a, b) => (b[1].percentage || 0) - (a[1].percentage || 0))
                .slice(0, 6);
            
            sorted.forEach(([fuel, info], i) => {
                const bgColor = i % 2 === 0 ? [255, 255, 255] : lightBg;
                doc.setFillColor(...bgColor);
                doc.rect(15, y, 85, 5, 'F');
                
                doc.setFontSize(8);
                doc.text(fuel.substring(0, 15), 18, y + 3.5);
                doc.text((info.mw || 0).toLocaleString(), 55, y + 3.5);
                doc.text(`${(info.percentage || 0).toFixed(1)}%`, 78, y + 3.5);
                y += 5;
            });
            
            // Total
            doc.setFillColor(...secondary);
            doc.setTextColor(255, 255, 255);
            doc.rect(15, y, 85, 5, 'F');
            doc.setFont('helvetica', 'bold');
            doc.text('Total Generation', 18, y + 3.5);
            doc.text(`${enhancedData.fuelMix.total_generation_gw?.toFixed(1) || 'N/A'} GW`, 65, y + 3.5);
            y += 8;
        }
        
        // ===========================================
        // CONNECTIVITY SECTION
        // ===========================================
        if (enhancedData.broadband) {
            doc.setTextColor(...text);
            if (y > 230) {
                doc.addPage();
                y = 20;
            }
            
            doc.setFillColor(...green);
            doc.rect(15, y, 180, 8, 'F');
            doc.setTextColor(255, 255, 255);
            doc.setFontSize(12);
            doc.setFont('helvetica', 'bold');
            doc.text('📶 Broadband Connectivity', 20, y + 6);
            y += 12;
            
            doc.setTextColor(...text);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            
            const bb = enhancedData.broadband;
            doc.text(`Coverage Tier: ${bb.coverage_tier || 'Unknown'}`, 15, y);
            y += 6;
            doc.text(`Max Speed: ${bb.max_download_mbps || 'N/A'}/${bb.max_upload_mbps || 'N/A'} Mbps (Down/Up)`, 15, y);
            y += 6;
            doc.text(`Providers: ${bb.provider_count || 0} available`, 15, y);
            y += 6;
            
            // Technology availability
            const techs = [];
            if (bb.has_fiber) techs.push('Fiber');
            if (bb.has_cable) techs.push('Cable');
            if (bb.has_5g_fixed) techs.push('5G Fixed');
            doc.text(`Technologies: ${techs.length > 0 ? techs.join(', ') : 'Unknown'}`, 15, y);
            y += 8;
        }
        
        // ===========================================
        // ENVIRONMENTAL COMPLIANCE SECTION
        // ===========================================
        if (enhancedData.epaFacilities) {
            if (y > 240) {
                doc.addPage();
                y = 20;
            }
            
            doc.setFillColor(16, 185, 129);
            doc.rect(15, y, 180, 8, 'F');
            doc.setTextColor(255, 255, 255);
            doc.setFontSize(12);
            doc.setFont('helvetica', 'bold');
            doc.text('🌿 Environmental Data (EPA)', 20, y + 6);
            y += 12;
            
            doc.setTextColor(...text);
            doc.setFontSize(11);
            doc.setFont('helvetica', 'normal');
            
            doc.text(`EPA-Registered Facilities in ${enhancedData.epaFacilities.state || 'State'}: ${enhancedData.epaFacilities.total}`, 15, y);
            y += 6;
            if (enhancedData.epaFacilities.nearby > 0) {
                doc.text(`Facilities within 50km: ${enhancedData.epaFacilities.nearby}`, 15, y);
                y += 6;
            }
            y += 4;
        }
        
        return y;
    }
    
    /**
     * Hook into existing PDF generation
     */
    function enhancePDFGeneration() {
        // Store reference to original generateReport if it exists
        if (window.SiteAnalyzer && window.SiteAnalyzer.generateReport) {
            const originalGenerateReport = window.SiteAnalyzer.generateReport.bind(window.SiteAnalyzer);
            
            window.SiteAnalyzer.generateReport = async function(siteData) {
                // Fetch enhanced data
                console.log('📄 Fetching enhanced data for PDF...');
                const enhancedData = await fetchEnhancedSiteData(
                    siteData.lat,
                    siteData.lng,
                    siteData.state
                );
                
                // Add to siteData
                siteData.enhancedData = enhancedData;
                
                // Generate PDF with original function
                const pdfBlob = await originalGenerateReport(siteData);
                
                console.log('✅ Enhanced PDF generated with Grid, Energy, FCC, EPA data');
                return pdfBlob;
            };
            
            console.log('✅ PDF Enhancement module loaded - will add Grid, Energy, EPA, FCC data');
        } else {
            // Try again in 500ms if SiteAnalyzer not ready
            setTimeout(enhancePDFGeneration, 500);
        }
    }
    
    // Expose functions globally
    window.DCHubPDFEnhancement = {
        fetchEnhancedSiteData,
        addEnhancedSectionsToPDF,
        stateToISO
    };
    
    // Initialize when DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', enhancePDFGeneration);
    } else {
        enhancePDFGeneration();
    }
    
})();
