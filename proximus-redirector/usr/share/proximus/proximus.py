#!/usr/bin/python -u

import sys,string
import MySQLdb
import MySQLdb.cursors
import os, signal
import socket
import syslog
import pprint # for debugging

import urlparse
import re
import base64
import smtplib
from email.MIMEText import MIMEText

from apscheduler.scheduler import Scheduler


config = {}
config_filename = "/etc/proximus/proximus.conf"
passthrough_filename = "/etc/proximus/passthrough"

# define globaly used variables
settings = {}
request = {'sitename':None, 'sitename_save':None, 'protocol':None, 'siteport':None, 'src_address':None, 'url':None, 'redirection_method':None, 'id':None }
user = {'ident':None, 'id':None, 'username':None, 'location_id':None, 'group_id':None, 'emailaddress':None }

class Proximus:
   def __init__(self):
      global db_cursor, config, settings

      # configure syslog
      syslog.openlog('proximus',syslog.LOG_PID,syslog.LOG_LOCAL5)
      self.stdin   = sys.stdin
      self.stdout  = sys.stdout

      # Get the fully-qualified name.
      hostname = socket.gethostname()
      fqdn_hostname = socket.getfqdn(hostname)

      # read config file and connect to the database
      self.read_configfile()
      self.db_connect()

      # prepare socket
      self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      self.s.setblocking(False)

      # Start the scheduler
      self.sched = Scheduler()
      self.sched.start()
      # Schedule job_function to be called
      self.job_bind = self.sched.add_interval_job(self.job_testbind, seconds=5)

      # Get settings from db and catch error if no settings are stored
      try:
         # Get proxy specific settings
         db_cursor.execute ("SELECT location_id, redirection_host, smtpserver, admin_email, admincc \
                           FROM proxy_settings \
                           WHERE fqdn_proxy_hostname = %s", ( fqdn_hostname ))
         query = db_cursor.fetchone()
         settings = query

         # Get global settings
         db_cursor.execute ("SELECT name, value FROM global_settings")
         query = db_cursor.fetchall()
         for row in query:
            settings[row['name']] = row['value']

         # combine with settings from configfile
         settings = dict(settings, **config)
         #pprint.pprint(settings)  ## debug 
      
      # catch error if no settings are stored;
      # and activate passthrough mode
      except :
         error_msg = "ERROR: please make sure that a config for this node is stored in the database. Table-name: proxy_settings - Full qualified domain name: "+fqdn_hostname
         self.log("ERROR: activating passthrough-mode until config is present")
         self.log(error_msg)
         self._writeline(error_msg)
         config['passthrough'] = True

   def db_connect(self):
      global db_cursor, config

      try:
         conn = MySQLdb.connect (host = config['db_host'],
            user = config['db_user'],
            passwd = config['db_pass'],
            db = config['db_name'], cursorclass=MySQLdb.cursors.DictCursor)
         db_cursor = conn.cursor ()
      except MySQLdb.Error, e:
         error_msg = "ERROR: please make sure that database settings are correctly set in "+config_filename
         self.log("ERROR: activating passthrough-mode until config is present")
         config['passthrough'] = True

         self.log(error_msg)
         self._writeline(error_msg)

   def read_configfile(self):
      global config
      try:
         config_file = open(config_filename, 'r')
      except :
         error_msg = "ERROR: config file not found: "+config_filename
         self.log(error_msg)
         self._writeline(error_msg)
         sys.exit(1)

      for line in config_file:
         # Get rid of \n
         line = line.rstrip()
         # Empty?
         if not line:
            continue
         # Comment?
         if line.startswith("#"):
            continue
         (name, value) = line.split("=")
         name = name.strip()
         config[name] = value
      #print config
      config_file.close()

      if os.path.isfile(passthrough_filename) :
         self.log("Warning, file: "+passthrough_filename+" exists; Passthrough mode activated")
         config['passthrough'] = True
      else :
         config['passthrough'] = False

      # set defaults
      if not config.has_key("web_path") :
         config['web_path'] = "/proximus"

      # do some converting
      if config.has_key("debug") :
         config['debug'] = int(config['debug'])
      else :
         config['debug'] = 0

   def job_testbind(self):
      try:
         self.s.bind(("127.0.0.1", 34563))
         self.s.listen(1)

         # deactivate bindtest job
         self.sched.unschedule_job(self.job_bind)
         # Schedule job_function to be called every two hours
         self.sched.add_interval_job(self.job_update, seconds=2)
         # remove the previous scheduled job
         self.log("I'm now the master process!")
      except socket.error, e:
         None


   def job_update(self):
      #self.log("running.......")
      None

   def _readline(self):
      "Returns one unbuffered line from squid."
      return self.stdin.readline()[:-1]

   def _writeline(self,s):
      self.stdout.write(s+'\n')
      self.stdout.flush()

   def run(self):
      global config, settings
      self.log("started")
      self.req_id = 0

      line = self._readline()
      while line:
         if config['passthrough'] == True :
            self._writeline("")
         else:
            self.req_id += 1
            if config['debug'] > 0 :
               self.log("Req  " + str(self.req_id) + ": " + line)
            response = self.check_request(line)
            self._writeline(response)
            if config['debug'] > 0 :
               self.log("Resp " + str(self.req_id) + ": " + response)
         line = self._readline()

   def log(s, str):
      syslog.syslog(syslog.LOG_DEBUG,str)



   ################
   ################
   ## Request processing
   ########
   ########

   # called when a site is blocked
   def deny(s):
      return "302:http://%s%s/forbidden.html" % ( settings['redirection_host'], settings['web_path'] )

   # called when access to a site is granted
   def grant(s):
      return ""

   # called when a request has to be learned
   def learn(s):
      global settings, request, user
      db_cursor = settings['db_cursor']
      # check if site has already been learned
      db_cursor.execute ("SELECT id \
                        FROM logs \
                        WHERE \
                           user_id = %s \
                           AND protocol = %s \
                           AND source != %s \
                           AND \
                              ( sitename = %s OR \
                              %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
                        ", (user['id'], request['protocol'], "REDIRECT", request['sitename'], request['sitename']))
      dyn = db_cursor.fetchone()
      if (dyn == None) :
         db_cursor.execute ("INSERT INTO logs (sitename, ipaddress, user_id, location_id, protocol, source, created) \
                           VALUES (%s, %s, %s, %s, %s, %s, NOW()) \
                  ", (request['sitename_save'], request['src_address'], user['id'], settings['location_id'], request['protocol'], "LEARN"))
      else :
         request['id'] = dyn['id']
         db_cursor.execute ("UPDATE logs SET hitcount=hitcount+1 \
                              WHERE id = %s ", ( request['id'] ) )


   # checks if a redirect has been logged and writes it into the db if not..
   def redirect_log(s):
      global db_cursor, settings, request, user

      db_cursor.execute ("INSERT INTO logs (sitename, ipaddress, user_id, protocol, location_id, source, created, hitcount) \
                           VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s) \
                         ON DUPLICATE KEY UPDATE hitcount=hitcount+1 \
                  ", (request['sitename_save'], request['src_address'], user['id'], request['protocol'], settings['location_id'], "REDIRECT", 1 ))
      request['id'] = db_cursor.lastrowid


   # checks if a redirect has been logged and writes it into the db if not..
   def redirect_log_hit(s, id):
      global db_cursor, settings, request, user
      db_cursor.execute ("UPDATE logs SET hitcount=hitcount+1 WHERE id = %s", (request['id']))


   # send redirect to the browser
   def redirect_send(s):
      global db_cursor, settings, request, user

      if request['protocol'] == "SSL" :
         # default redirection method - if not further specified
         return "302:http://%s%s/proximus.php?site=%s&id=%s&url=%s" % (settings['redirection_host'], settings['web_path'], request['sitename_save'], request['id'], base64.b64encode("https://"+request['sitename']))

      else:
         # its http
         return "302:http://%s%s/proximus.php?site=%s&id=%s&url=%s" % (settings['redirection_host'], settings['web_path'], request['sitename_save'], request['id'], base64.b64encode(request['url']))


   # called when a request is redirected
   def redirect(s):
      global db_cursor, settings, request, user

      if request['sitename'].startswith(settings['retrain_key']) :
         key = settings['retrain_key']
         request['sitename'] = re.sub("^"+key, "", request['sitename'])
         request['sitename_save'] = re.sub("^www\.", "", request['sitename'])
         request['url'] = re.sub(key+request['sitename'], request['sitename'], request['url'])
         s.redirect_log()
         return s.redirect_send()


      ######
      ## check if user has the right to access this site, if not check against shared-subsites if enabled 
      ##

      # check if user has already added site to dynamic rules
      db_cursor.execute ("SELECT sitename, id, source \
                        FROM logs \
                        WHERE \
                              user_id = %s \
                              AND protocol = %s \
                              AND source != %s \
                           AND \
                              ( sitename = %s OR \
                              %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
                        ", (user['id'], request['protocol'], "REDIRECT", request['sitename'], request['sitename']))
      dyn = db_cursor.fetchone()
      if (dyn != None) :   # user is allowed to access this site
         if settings['debug'] >= 2 :
            s.log("Req  "+ str(s.req_id) +": REDIRECT; Log found; " + pprint.pformat(dyn) )
         request['id'] = dyn['id']
         s.redirect_log_hit(request['id'])
         return s.grant()
      elif settings['subsite_sharing'] == "own_parents" :    # check if someone else has already added this site as a children
         db_cursor.execute ("SELECT log2.sitename AS sitename, log2.id AS id \
                           FROM logs AS log1, logs AS log2 \
                           WHERE \
                                 log1.parent_id = log2.id \
                                 AND log1.protocol = %s \
                                 AND log1.source != %s \
                              AND \
                                 ( log1.sitename = %s OR \
                                 %s RLIKE CONCAT( '.*[[.full-stop.]]', log1.sitename, '$' )) \
                           ", (request['protocol'], "REDIRECT", request['sitename'], request['sitename']))
         rows1 = db_cursor.fetchall()
         db_cursor.execute ("SELECT sitename, id \
                           FROM logs \
                           WHERE \
                                 user_id = %s \
                                 AND parent_id IS NULL \
                                 AND source != %s \
                           ", (user['id'], "REDIRECT"))
         rows2 = db_cursor.fetchall()

         for row1 in rows1:
            for row2 in rows2:
               if row1['sitename'] == row2['sitename'] :
                  if settings['debug'] >= 2 :
                     s.log("Debug REDIRECT; Log found with subsite sharing - own_parents; Log-id="+str(rows1['id']))
                  return s.grant()
      elif settings['subsite_sharing'] == "all_parents" :  # check if someone else has already added this site as a children 
         db_cursor.execute ("SELECT sitename, id \
                           FROM logs \
                           WHERE \
                                 parent_id IS NOT NULL \
                                 AND source != %s \
                              AND \
                                 ( sitename = %s OR \
                                 %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
                           ", ("REDIRECT", request['sitename'], request['sitename']))
         all = db_cursor.fetchone()
         if (all != None) :
            if settings['debug'] >= 2 :
               s.log("Debug REDIRECT; Log found with subsite sharing - all_parents; Log-id="+str(all['id']))
            return s.grant()

      # if we get here user is not yet allowed to access this site
      if settings['debug'] >= 2 :
         s.log("Debug REDIRECT; No log found; DENY")
      # log request
      s.redirect_log()
      return s.redirect_send()
      

   def send_mail(s, subject, body):
      global settings, user
      smtp = smtplib.SMTP(settings['smtpserver'])
      msg = MIMEText(body)
      msg['Subject'] = subject
      msg['From'] = "ProXimus"
      msg['To'] = user['email']
      if settings['admincc'] == 1 :
         msg['Cc'] = settings['admin_email']
         smtp.sendmail(settings['admin_email'], settings['admin_email'], msg.as_string())
      smtp.sendmail(settings['admin_email'], user['emailaddress'], msg.as_string())
      smtp.close()


   def deny_mail_user(s):
      global settings, user, request

      # if user doesn't have an email address skip the part below
      if user['emailaddress'] == "":
         return s.deny()

      # check if mail has already been sent
      db_cursor = settings['db_cursor']
      db_cursor.execute ("SELECT id  \
                           FROM maillog \
                           WHERE \
                              user_id = %s \
                              AND (HOUR(NOW()) - HOUR(sent)) <= %s \
                              AND \
                                 ( sitename = %s OR \
                                 %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
                              AND \
                                 ( protocol = %s OR \
                                 protocol = '*' ) \
                              ", (user['id'], settings['mail_interval'], request['sitename'], request['sitename'], request['protocol']) )
      result = db_cursor.fetchone()
      if (result == None) : # no mail has been sent recently
         if request['protocol'] == "SSL" :
            scheme = "https"
         else :
            scheme = "http"

         s.send_mail('Site '+request['sitename']+' has been blocked', "Dear User! \n\nYour request to "+scheme+"://"+request['sitename']+" has been blocked. \n\nIf you need access to this page please contact your Administrator.\n\nProXimus")
         
         # log that a mail has been sent
         db_cursor.execute ("INSERT INTO maillog (sitename, user_id, protocol, sent) \
                              VALUES (%s, %s, %s, NOW()) ", (request['sitename_save'], user['id'], request['protocol']))
         dyn = db_cursor.fetchone()
      return s.deny()


   def parse_line(s, line):
      global request, user
      # clear previous request data
      request = {}
      uparse, ujoin = urlparse.urlparse , urlparse.urljoin

      withdraw = string.split(line)
      if len(withdraw) >= 5:
         # all needed parameters are given
         url = withdraw[0]
         src_address = withdraw[1]
         ident = withdraw[2]
         method = withdraw[3]
      else:
         # not enough parameters - deny
         return False

      # scheme://host/path;parameters?query#fragment
      (scheme,host,path,parameters,query,fragment) = uparse(url)

      # prepare username
      user['ident'] = ident.lower()
      if settings['regex_cut'] != "" :
         user['ident'] = re.sub(settings['regex_cut'], "", user['ident'])

      # remove "/-" from source ip address
      request['src_address'] = re.sub("/.*", "", src_address)
      request['url'] = url

      if method == "CONNECT" :
         """it's ssl"""
         request['protocol'] = "SSL"
         request['sitename'] = scheme
         request['siteport'] = path
      else:
         """it' http"""
         request['protocol'] = "HTTP"
         request['sitename'] = host.split(":", 1)[0]
         try:
            request['siteport'] = host.split(":", 1)[1]
         except IndexError,e:
            request['siteport'] = "80"
      request['sitename_save'] = re.sub("^www\.", "", request['sitename'])


   def fetch_userinfo(s, ident):
      global db_cursor, settings, user

      if ident != "-" :
         # get user
         try:
            db_cursor.execute ("SELECT id, username, location_id, emailaddress, group_id FROM users WHERE username = %s AND active = 'Y'", ident)
            user = db_cursor.fetchone()
            user['emailaddress'] = user['emailaddress'].rstrip('\n')
         except TypeError:
            user = None
      else :
         user = None

      #pprint.pprint(user)   ## debug
      if settings['debug'] >= 2 :
         if user != None :
            s.log("Req  "+ str(s.req_id) +": User found; " + pprint.pformat(user) )
         else :
            s.log("Req  "+ str(s.req_id) +": No user found; ident="+ident)
    
      # make all vars lowercase to make sure they match
      #sitename = escape(sitename)
      #ident = escape(ident.lower())
      #src_address = escape(src_address)


   # tests if a ip address is within a subnet
   def addressInNetwork(s, ip, net):
      import socket,struct
      try:
         ipaddr = int(''.join([ '%02x' % int(x) for x in ip.split('.') ]), 16)
         netstr, bits = net.split('/')
         netaddr = int(''.join([ '%02x' % int(x) for x in netstr.split('.') ]), 16)
         mask = (0xffffffff << (32 - int(bits))) & 0xffffffff
         return (ipaddr & mask) == (netaddr & mask)
      except ValueError:
         return False;


   def check_request(s, line):
      global db_cursor, settings, request, user

      #bdb_cursor = settings['db_cursor']

      if s.parse_line(line) == False:
         return s.deny()
      s.fetch_userinfo(user['ident'])

      # allow access to to proximuslog website
      if request['sitename'] == settings['redirection_host'] :
         return s.grant()


      ######
      ## Global blocked network check
      ##
      db_cursor.execute ("SELECT network \
               FROM blockednetworks \
               WHERE \
                     ( location_id = %s \
                     OR location_id = 1 ) ",
               (settings['location_id'] ))
      rows = db_cursor.fetchall()
      for row in rows:
         if request['src_address'] == row['network'] :
            return s.deny();
         if s.addressInNetwork( request['src_address'] ,  row['network'] ) :
            return s.deny();


      ######
      ## Global no-auth check
      ##
      if user == None :
         # since squid is configured to require user auth
         # and no user identification is sent the site must be in the no-auth table
         if settings['debug'] >= 2 :
            s.log("Req  "+ str(s.req_id) +": ALLOW - Request with no user-id - looks like a NoAuth rule ;-)")
         return s.grant()
      #else :
         # actually this should not be nessecary - the browser should never
         # send user identification if the site is in the no-auth table;
         # in case it does we have that query
         # so commenting this out now
         #db_cursor.execute ("SELECT sitename, protocol  \
         #                     FROM noauth_rules \
         #                     WHERE \
         #                           ( sitename = %s OR \
         #                           %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
         #                        AND \
         #                           ( protocol = %s OR \
         #                           protocol = '*' )", (request['sitename'], request['sitename'], request['protocol']) )
         #rows = db_cursor.fetchall()
         #for row in rows:
         #   return grant()


      ######
      ## retrieve rules for user
      ##

      # check if we could retrieve user information
      if user['id'] != None :
         db_cursor.execute ("SELECT id, sitename, policy, location_id, group_id, priority, description \
                  FROM rules \
                  WHERE \
                        ( group_id = %s \
                        OR group_id = 0 ) \
                     AND \
                        ( location_id = %s \
                        OR location_id = 1 ) \
                     AND \
                        ( sitename = %s OR \
                        %s RLIKE CONCAT( '.*[[.full-stop.]]', sitename, '$' )) \
                     AND \
                        ( protocol = %s OR \
                        protocol = '*' ) \
                     AND \
                        ( starttime is NULL AND endtime is NULL OR \
                        starttime <= NOW() AND NOW() <= endtime ) \
                  ORDER BY priority DESC, location_id",
                  (user['group_id'], user['location_id'], request['sitename'], request['sitename'], request['protocol']))
      rules = db_cursor.fetchall()
      for rule in rules:
         if settings['debug'] >= 2 :
            s.log("Req  "+ str(s.req_id) +": Rule found; " + pprint.pformat(rule))
         if rule['policy'] == "ALLOW" :
            return s.grant()
         elif rule['policy'].startswith("REDIRECT") :
            request['redirection_method'] = rule['policy']
            return s.redirect()
         elif rule['policy'] == "DENY_MAIL" :
            return s.deny_mail_user()
         elif rule['policy'] == "DENY" :
            return s.deny()
         elif rule['policy'] == "LEARN" :
            s.learn()
            return s.grant()

      if settings['debug'] >= 2 :
         s.log("Req  "+ str(s.req_id) +": no rule found; using default deny")

      # deny access if the request was not accepted until this point ;-)
      return s.deny()



if __name__ == "__main__":
   sr = Proximus()
   sr.run()

