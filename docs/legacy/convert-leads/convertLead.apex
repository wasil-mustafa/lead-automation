// Update lead to pass validation rules
Lead l = [SELECT Id, Name FROM Lead WHERE Id = '00QMz00000iEVaTMAW' LIMIT 1];
l.Street = ',';
l.KN_Employee__c = '11-50';
l.KN_NoOwnDBID__c = true;
update l;
System.debug('Lead updated: ' + l.Name);

// Convert lead
Database.LeadConvert lc = new Database.LeadConvert();
lc.setLeadId(l.Id);
lc.setOwnerId('005Mz00000XvtQzIAJ');
lc.setConvertedStatus('Qualified');
lc.setDoNotCreateOpportunity(true);

Database.LeadConvertResult result = Database.convertLead(lc);

if (result.isSuccess()) {
    System.debug('SUCCESS - Lead: ' + l.Name);
    System.debug('Account ID: ' + result.getAccountId());
    System.debug('Contact ID: ' + result.getContactId());
} else {
    for (Database.Error err : result.getErrors()) {
        System.debug('FAILED - Lead: ' + l.Name + ' (' + l.Id + ') | ' + err.getStatusCode() + ' : ' + err.getMessage());
    }
}