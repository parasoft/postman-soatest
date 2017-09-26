package postman_to_soatest;

import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Base64;

import com.jayway.jsonpath.JsonPath;
import com.jayway.jsonpath.PathNotFoundException;

public class Importer  {
	private final static String TST_EXTENSION = "/soavirt/api/v5/files/tsts";
	private final static String REST_EXTENSION = "/soavirt/api/v5/tools/restClients";
	private final static String TST_SUITE_EXTENSION = "/soavirt/api/v5/suites/testSuites";
	static String ENDPOINT = "";
	
	static String tstSuiteId = "";
	static boolean hasSecurity = false;
	static boolean hasHeaders = false;
	static boolean verbose = false;
	
	public static void main(String args[]) throws Exception 
	{
		try{
			@SuppressWarnings("unused")
			String temp = args[0];
		}
		catch(Exception e){
			helpOutput();
			return;
		}
		if(args[0].equals("-help"))
		{
			helpOutput();
			
			return;
		}
		if(args[0].equals("-v"))
			verbose = true;
		else if(args[0].startsWith("-"))
		{
			System.out.println("The option you are trying to specify does not exist.\n");
			helpOutput();
			
			return;
		}
		
		BufferedReader in = null;
		try{
			//in = new BufferedReader(new FileReader("C:/Users/tmoore/Desktop/Postman Echo.postman_collection.json"));
			if(verbose)
				in = new BufferedReader(new FileReader(args[1]));
			else
				in = new BufferedReader(new FileReader(args[0]));
		}
		catch(Exception e){
			System.out.println(e.toString() + "\n");
			helpOutput();
			return;
		}
		
		ENDPOINT = "http://localhost:9080";
		
		if(!verbose && args.length > 1)
			ENDPOINT = args[1];
		else if(verbose && args.length > 2)
			ENDPOINT = args[2];
		
		String temp = in.readLine();
		String json = "";
		
		while(temp != null)
		{
			//System.out.println(temp);
			json = json + temp;
			temp = in.readLine();
		}
		
		String tst = "{\"name\": \""+ JsonPath.parse(json).read("$.info.name") + "\",\"parent\": {\"id\": \"TestAssets\"}}"; 
		
		//System.out.println(tst + "\n\n" + ENDPOINT + TST_EXTENSION);
		String values = "";
		try{
			values = sendRequest(tst, ENDPOINT + TST_EXTENSION);
		}
		catch(ConnectException e)
		{
			//System.out.println(e.toString());
			System.out.println("Your SOAvirt server is not running. Please start it and try again.");
			return;
		}
		catch(IOException e)
		{
			//System.out.println(e.toString());
			System.out.println("A .tst file with that name already exists. Please remove/rename it and try again.");
			return;
		}
		
		String tstId = JsonPath.parse(values.toString()).read("$.id");
		
		
		int counter = 0;
		while(counter < (int)JsonPath.parse(json).read("$.item.length()"))
		{
			boolean folders = true;
			try{
				Integer.parseInt(JsonPath.parse(json).read("$.item.["+counter+"].item.length()"));
			}
			catch(Exception e)
			{
				if(e.toString().contains("Missing property in path $['item']"))
				{
					folders = false;
				}
			}
			if(folders)
			{
				String tstSuite = "{\"parent\": {\"id\":\""+tstId+"/Test Suite\"},\"name\":\""+JsonPath.parse(json).read("$.item["+counter+"].name")+"\"}";
				tstSuiteId = tstId +"/Test Suite/" + JsonPath.parse(json).read("$.item["+counter+"].name");
				
				
				sendRequest(tstSuite, ENDPOINT + TST_SUITE_EXTENSION);
				
				int counter2 = 0;
				while(counter2 < (int)JsonPath.parse(json).read("$.item["+counter+"].item.length()"))
				{			
					//System.out.println(json);
					helper(json, counter, counter2);
					
					counter2++;
				}
			}
			else
			{				
				tstSuiteId = tstId +"/Test Suite";
				
				helper(json, counter, -1);
			}
			counter++;
			
		}
		System.out.println("Tests successfully imported!");
	}
	
	private static void helper(String json, int a, int b) throws Exception
	{
		String newRequest = requiredFields(json, a, b);		
		
		String httpOptions = "\"httpOptions\":{\"transport\":{\"http10\":{";
		
		hasSecurity = false;
		hasHeaders = false;
		
		newRequest = newRequest + httpOptions;
		
		newRequest = newRequest.concat(security(json, a, b));
		
		newRequest = newRequest.concat(headers(json, a, b));
		newRequest = newRequest + "}}}";
		
		newRequest = newRequest.concat(body(json, a, b));
		
		newRequest = newRequest.concat("}");
		
		//System.out.println(newRequest);
		sendRequest(newRequest, ENDPOINT + REST_EXTENSION);
		
		//addScripts(json, a, b);
	}
	
	/*private static void addScripts(String json, int a, int b) throws Exception {
		String newRequest = "{";
		sendRequest(newRequest, ENDPOINT + SCRIPT_EXTENSION);
	}*/

	private static String getItem(int a, int b)
	{
		if(b == -1)
			return "$.item["+a+"]";
		else
			return "$.item["+a+"].item["+b+"]";
	}
	
	private static String requiredFields(String json, int a, int b)
	{
		String items = getItem(a, b);
		
		String requestType = JsonPath.parse(json).read(items + ".request.method");
		String requestName = JsonPath.parse(json).read(items+".name").toString();
		String newRequest = "";
		
		if(!requestType.equals("POST") && !requestType.equals("PUT") && !requestType.equals("GET") && !requestType.equals("OPTIONS") &&
				!requestType.equals("HEAD") && !requestType.equals("TRACE") && !requestType.equals("DELETE"))
			newRequest = "{\"header\":{\"method\":{\"value\":\""+requestType+"\",\"methodType\":\"CUSTOM\"}},";
		else
			newRequest = "{\"header\":{\"method\":{\"methodType\":\""+requestType+"\"}},";
		
		if(((JsonPath.parse(json).read(items + ".request.url")).toString()).contains("raw="))
		{
			newRequest = newRequest.concat("\"resource\":{\"type\":\"literalText\",\"literalText\":{\"fixed\":\"" +
					JsonPath.parse(json).read(items+".request.url.raw") + "\"}},");
		}
		else
		{
			newRequest = newRequest.concat("\"resource\":{\"type\":\"literalText\",\"literalText\":{\"fixed\":\"" +
					JsonPath.parse(json).read(items+".request.url") + "\"}},");
		}
		
		newRequest = newRequest.concat("\"parent\":{\"id\":\"" + tstSuiteId + "\"},");
		
		return newRequest.concat("\"name\":\"" + requestName+ "\",");
	}
	
	private static String security(String json, int a, int b) throws Exception
	{
		String items = getItem(a, b);
		String newRequest = "";
		for(int i = 0; i < (int)JsonPath.parse(json).read(items + ".request.header.length()"); i++)
		{
			if(JsonPath.parse(json).read(items + ".request.header["+i+"]").toString().contains("Authorization") &&
					JsonPath.parse(json).read(items + ".request.header["+i+"].value").toString().startsWith("Basic"))
			{
				if(i > 0)
					newRequest = newRequest + ",";
				
				String security = "\"security\": {\"httpAuthentication\": {\"performAuthentication\": {"
						+ "\"value\": {\"useGlobal\": false,\"authenticationType\":{\"basic\": "
						+"{\"username\": {\"fixed\":\"";
				
				String encoded = JsonPath.parse(json).read(items + ".request.header["+i+"].value").toString();
				encoded = encoded.substring(6);
				byte[] toBeDecoded = encoded.getBytes("UTF-8");
				byte[] decoded = Base64.getDecoder().decode(toBeDecoded);
				String userAndPass = new String(decoded);
				String user = userAndPass.substring(0, userAndPass.indexOf(":"));
				String pass = userAndPass.substring(userAndPass.indexOf(":")+1);
				
				security = security + user+"\"},\"password\":{\"masked\":\""+
						pass+"\"}}}},\"enabled\":true}}}";
				
				newRequest = newRequest + security;
				
				hasSecurity = true;
			}
		}
		return newRequest;
	}
	
	private static String headers(String json, int a, int b)
	{
		String items = getItem(a, b);
		String headers = "\"httpHeaders\":{\"httpHeadersTable\":{\"rows\":[";
		String newRequest = "";
		for(int i = 0; i < (int)JsonPath.parse(json).read(items + ".request.header.length()"); i++)
		{
			if(!(JsonPath.parse(json).read(items + ".request.header["+i+"]").toString()).contains("Authorization") &&
					!(JsonPath.parse(json).read(items + ".request.header["+i+"]").toString()).contains("Content-Type"))
			{
				if(hasSecurity && !hasHeaders)
				{
					newRequest = newRequest + ",";
					hasHeaders = true;
				}
				String name = "{\"name\":\""+JsonPath.parse(json).read(items + ".request.header["+i+"].key")+"\",";
				String value = "\"value\":{\"fixed\":\""+JsonPath.parse(json).read(items + ".request.header["+i+"].value")+"\"}}";
				headers = headers + name + value;
				
				newRequest = newRequest + headers;
				if(i+1 == (int)JsonPath.parse(json).read(items + ".request.header.length()")){
					newRequest = newRequest + "]}}";
				}
			}
		}
		return newRequest;
	}
	
	private static String body(String json, int a, int b)
	{
		String items = getItem(a, b);
		String requestType = JsonPath.parse(json).read(items + ".request.method");
		String newRequest = "";
		if(requestType.equalsIgnoreCase("POST") || requestType.equalsIgnoreCase("PUT") || requestType.equalsIgnoreCase("DELETE") ||
				requestType.equalsIgnoreCase("CUSTOM"))
		{
			String payload = ",\"payload\":{\"input\":{\"literal\":{\"text\": \"";
			try{
				if(JsonPath.parse(json).read(items + ".request.body.mode").toString().equals("raw"))
				{
					String s = JsonPath.parse(json).read(items + ".request.body.raw");
					s = s.replaceAll("\"", "\\\\\"").replaceAll("\\\\\\\\", "\\\\\\\\\\\\").replaceAll("\n", "");
					payload = payload + s + "\"}},";
				}
				else if(JsonPath.parse(json).read(items + ".request.body").toString().contains("\"urlencoded\":"))
				{
					for(int i = 0; i < (int)JsonPath.parse(json).read(items + ".request.body.urlencoded.length()"); i++)
					{
						payload = payload + JsonPath.parse(json).read(items + ".request.body.urlencoded.["+i+"].key") + " ";
						payload = payload + JsonPath.parse(json).read(items + ".request.body.urlencoded.["+i+"].value") + " ";
						payload = payload + JsonPath.parse(json).read(items + ".request.body.urlencoded.["+i+"].type") + "\"}},";
					}
				}
				else if(JsonPath.parse(json).read(items + ".request.body").toString().equals("{}"))
				{
					payload = payload + "\"}},";
				}
				else
				{
					System.out.println("Body Type for operation \""+ JsonPath.parse(json).read(items + ".name") +"\" is not supported. Please verify your test case data manually.");
					payload = payload + "{\\\"Body type not supported\\\":\\\"Please verify your data manually\\\"}\"}},";
				}
			}
			catch(PathNotFoundException e)
			{
				System.out.println(e.toString() + "\n");
				return newRequest;
				
			}
			
			newRequest = newRequest + payload + "\"contentType\": \"";
				
			String type =  JsonPath.parse(json).read(items + ".request.header[?(@.key=='Content-Type')].value").toString();
			type = type.replaceAll("\\[(.*?)\\]", "$1").replaceAll("\\\\", "").replaceAll("\"", "");
			
			newRequest = newRequest + type + "\"}";
		}
		return newRequest;
	}
	
	private static String sendRequest(String request, String endpoint) throws Exception
	{
		URL url = new URL(endpoint);
		HttpURLConnection urlConnection = (HttpURLConnection) url.openConnection();
		
		urlConnection.setRequestMethod("POST");
		urlConnection.setDoOutput(true);
		
		urlConnection.addRequestProperty("Content-Type", "application/json");
		urlConnection.addRequestProperty("Accept", "application/json");
				
		if(verbose)
			System.out.println("Request: " + request);
		if(request != null)
		{
			urlConnection.setRequestProperty("Content-Length", Integer.toString(request.length()));
			urlConnection.getOutputStream().write(request.getBytes("UTF-8"));
		}
		
		BufferedReader br = new BufferedReader(new InputStreamReader(urlConnection.getInputStream()));
		
		StringBuilder sb = new StringBuilder();
		String output;
		
		while((output = br.readLine()) != null)
		{
			sb.append(output);
		}
		if(verbose)
			System.out.println("Response: " + sb.toString() + "\n");
		
		urlConnection.disconnect();
		
		return sb.toString();
	}
	
	private static void helpOutput()
	{
		System.out.println("To import Postman services with this tool, simply run the jar"+
				"\nwith the filepath to the export to your collection as an argument.");
		System.out.println("\n\nExample: java -jar PostmanImporter.jar C:/tmp/postmanExports/myCollection.json\n");
		System.out.println("This jar assumes that the SOAVirt Server is running on your local machine at \nport 9080.\n");
		System.out.print("If this does not match the server you wish to upload to, you can supply the \nserver ");
		System.out.println("settings as a second argument.");
		System.out.println("\n\nExample: java -jar PostmanImporter.jar myCollection.json http://RemoteServer123:1111");
		System.out.println("\nAdditional Options:\n-v: Enables verbose mode");
		System.out.println("\nExample: java -jar PostmanImporter.jar -v myCollection.json");
	}
	
}
