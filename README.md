# Postman to SOAtest conversion tool

The tool should be packaged into a jar to be run, or run the Importer.java file directly through eclipse. The PostmanImporter folder cacn be imported directly into Eclipse as a java project.


This import tool requires the Server API license for SOAtest/Virtualize to be active, and the SOAtest/Virtualize server must be running.

==========
To import Postman services with this tool, simply run the jar
with the filepath to the export to your collection as an argument.


Example: java -jar PostmanImporter.jar C:/tmp/postmanExports/myCollection.json

This jar assumes that the SOAVirt Server is running on your local machine at 
port 9080.

If this does not match the server you wish to upload to, you can supply the 
server settings as a second argument.

Example: java -jar PostmanImporter.jar myCollection.json http://RemoteServer123:1111

Additional Options:
-v: Enables verbose mode

Example: java -jar PostmanImporter.jar -v myCollection.json
==========



Credit and documentation for JsonPath implementation goes to https://github.com/json-path/JsonPath
